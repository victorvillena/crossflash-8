"""Console progress for the flashers: per-component bar, overall percentage and an ETA.

On a terminal one status line is redrawn in place under the timestamped event log:

    slave  [#######.........]  45% 1363/3029 | total  15% | 0:28 elapsed ~3:12 left

When stdout is piped or logged, the bar is replaced by a plain line every 10 % of each
component, so logs stay readable.  Only ASCII and carriage returns are used (no ANSI), so
it works on any console.

The percentages count messages sent.  The ETA adds the remaining messages at the measured
send rate to the device's typical programming time for each component not yet accepted.
"""
from __future__ import annotations
import shutil
import sys
import time

# Typical device-side programming time after each component is sent (0=panel 1=master
# 2=slave), measured on a Cobalt 8 at 20 ms pacing.  Only used for the ETA.
PROGRAM_SECS = {0: 0.0, 1: 5.0, 2: 40.0}
BAR = 16                    # keeps the status line within 80 columns
REDRAW = 0.1                # seconds between status-line redraws on a terminal


def fmt_secs(s: float) -> str:
    s = max(0, int(round(s)))
    return f"{s // 60}:{s % 60:02d}"


def cc_text(num: int, val: int) -> str:
    note = {0x78: " (programming)", 0x77: " (done)"}.get(val, "") if num == 127 else ""
    return f"dev CC #{num} = {val:#04x}{note}"


class Progress:
    def __init__(self, sizes: dict[int, int], names: dict[int, str], pace: float, out=None):
        self.sizes = sizes
        self.names = names
        self.pace = pace
        self.out = out or sys.stdout
        self.tty = self.out.isatty()
        self.t0 = time.time()
        self.sent: set[int] = set()         # components fully sent (latest run)
        self.current: int | None = None     # component being sent
        self.count = 0                      # messages of `current` sent so far
        self.run_t0 = 0.0
        self.waiting: int | None = None     # component the device is programming
        self.wait_t0 = 0.0
        self.send_secs = 0.0                # finished runs, for the measured rate
        self.send_msgs = 0
        self.width = 0                      # length of the status line on screen
        self.last_draw = 0.0
        self.step = 0                       # last 10 % step printed (non-terminal)

    # ------------------------------------------------------------------ events
    def log(self, msg: str, stamp: bool = True) -> None:
        """Print an event line above the status line."""
        self._clear()
        print(f"[{time.time() - self.t0:7.1f}] {msg}" if stamp else msg, file=self.out)
        self.draw(force=True)

    def begin(self, t: int) -> None:
        self.current, self.count, self.run_t0 = t, 0, time.time()
        self.waiting = None
        self.sent.discard(t)
        self.step = 0
        self.draw(force=True)

    def advance(self, n: int) -> None:
        self.count = n
        self.draw()

    def end(self) -> float:
        """The current component is fully sent; returns how long sending it took."""
        t, dt = self.current, time.time() - self.run_t0
        self.send_secs += dt
        self.send_msgs += self.count
        self.sent.add(t)
        self.current = None
        self.waiting, self.wait_t0 = t, time.time()
        self.draw(force=True)
        return dt

    def close(self, keep: bool = True) -> None:
        """Remove the status line, or with `keep` leave it on screen (useful after a failure
        or Ctrl-C) and move below it."""
        if self.width:
            if keep:
                self.out.write("\n")
                self.width = 0
            else:
                self._clear()
            self.out.flush()

    # ------------------------------------------------------------------ figures
    def total_pct(self) -> int:
        done = sum(self.sizes[t] for t in self.sent)
        if self.current is not None:
            done += self.count
        return 100 * done // sum(self.sizes.values())

    def eta(self) -> float:
        now = time.time()
        secs, msgs = self.send_secs, self.send_msgs
        if self.current is not None:
            secs += now - self.run_t0
            msgs += self.count
        rate = secs / msgs if msgs >= 50 else self.pace
        left = 0.0
        for t, n in self.sizes.items():
            if t in self.sent:
                continue
            left += rate * (n - (self.count if t == self.current else 0))
            left += PROGRAM_SECS.get(t, 0.0)
        if self.waiting is not None:
            left += max(0.0, PROGRAM_SECS.get(self.waiting, 0.0) - (now - self.wait_t0))
        return left

    # ------------------------------------------------------------------ drawing
    def _line(self) -> str | None:
        now = time.time()
        tail = (f" | total {self.total_pct():3d}% | {fmt_secs(now - self.t0)} elapsed "
                f"~{fmt_secs(self.eta())} left")
        if self.current is not None:
            t, n = self.current, self.sizes[self.current]
            fill = BAR * self.count // n
            return (f"{self.names[t]:<6} [{'#' * fill}{'.' * (BAR - fill)}] "
                    f"{100 * self.count // n:3d}% {self.count:>{len(str(n))}}/{n}" + tail)
        if self.waiting is not None:
            return (f"{self.names[self.waiting]:<6} sent, device working "
                    f"{fmt_secs(now - self.wait_t0)}" + tail)
        return None

    def draw(self, force: bool = False) -> None:
        if not self.tty:
            self._log_step()
            return
        now = time.time()
        if not force and now - self.last_draw < REDRAW:
            return
        self.last_draw = now
        line = self._line()
        if line is None:
            return
        line = line[:shutil.get_terminal_size((80, 24)).columns - 1]
        self.out.write("\r" + line + " " * max(0, self.width - len(line)))
        self.out.flush()
        self.width = len(line)

    def _clear(self) -> None:
        if self.width:
            self.out.write("\r" + " " * self.width + "\r")
            self.width = 0

    def _log_step(self) -> None:
        if self.current is None:
            return
        step = 10 * self.count // self.sizes[self.current]
        if step > self.step:
            self.step = step
            print(f"[{time.time() - self.t0:7.1f}] {self._line()}", file=self.out)
