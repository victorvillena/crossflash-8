"""Serve a patched 3-component set to the Modal bootloader over ALSA raw-MIDI (Linux).

Implements the reverse-engineered update handshake:

    host -> dev   identify   F0 00 21 07 64 01 01 F7
    dev  -> host             F0 00 21 07 <model> 01 7D 00 04 01 01 F7
    host -> dev   start      F0 00 21 07 <model> 01 04 01 F7
    dev  -> host  request    F0 00 21 07 <model> 7C <type> F7      0=panel 1=master 2=slave
    host -> dev   image      the component's .syx, split on F7
    dev  -> host  progress   CC#127 = 0x78 (received) then 0x77 (installed)
    dev   reboots after all three are served

<model> is what the bootloader reports (0x19 Cobalt, 0x18 Argon, ...); the set being
served must carry the same model byte, or the flasher refuses before writing anything.

20 ms per message is the sweet spot: faster causes "missed firmware start packet".
"""
from __future__ import annotations
import argparse
import fcntl
import os
import select
import sys
import time

from . import syx
from .progress import Progress, cc_text, fmt_secs

IDENTIFY = bytes([0xF0, 0x00, 0x21, 0x07, 0x64, 0x01, 0x01, 0xF7])
NAMES = {0: "panel", 1: "master", 2: "slave"}


# ---------------------------------------------------------------- ALSA helpers
def _cards() -> dict[int, str]:
    out = {}
    try:
        for ln in open("/proc/asound/cards"):
            if ":" in ln and "[" in ln:
                idx = int(ln.split()[0])
                out[idx] = ln
    except OSError:
        pass
    return out


def find_port() -> str | None:
    """Any Modal 8-series USB-MIDI card.  The bootloader (update mode) presents as the
    stock product name; the running application presents as its own (possibly
    cross-flashed) name, so match all of them."""
    want = ("CARBON", "COBALT", "ARGON", "MODAL")
    for idx, desc in _cards().items():
        if any(w in desc.upper() for w in want):
            return f"hw:{idx},0,0"
    return None


def node_for(port: str) -> str:
    a = port.replace("hw:", "").split(",")
    return f"/dev/snd/midiC{a[0]}D{a[1] if len(a) > 1 else 0}"


def wwrite(fd: int, data: bytes) -> None:
    """Write a whole raw-MIDI message, honouring EAGAIN."""
    mv = memoryview(data)
    while mv:
        try:
            n = os.write(fd, mv)
            mv = mv[n:]
        except BlockingIOError:
            select.select([], [fd], [], 0.5)


# ------------------------------------------------------------------- protocol
class Session:
    def __init__(self, fd: int, images: dict[int, list[bytes]], pace_ms: float,
                 verbose: bool = False):
        self.fd = fd
        self.images = images
        self.pace = pace_ms / 1000.0
        self.verbose = verbose
        self.progress = Progress({t: len(d) for t, d in images.items()}, NAMES, self.pace)
        self.buf = b""
        self.served: set[int] = set()
        self.runs: dict[int, int] = {}
        self.pending: int | None = None
        self.saw_hello = False
        self.model: int | None = None

    def feeds(self, data: bytes):
        self.buf += data
        b = self.buf
        i = 0
        while i < len(b):
            x = b[i]
            if x == 0xF0:
                j = b.find(b"\xF7", i + 1)
                if j < 0:
                    break
                yield ("sysex", b[i:j + 1])
                i = j + 1
            elif x >= 0xF8:
                i += 1
            elif x & 0x80:
                hi = x & 0xF0
                need = {0x80: 2, 0x90: 2, 0xA0: 2, 0xB0: 2, 0xC0: 1, 0xD0: 1, 0xE0: 2}.get(hi)
                if need is None or i + 1 + need > len(b):
                    i += 1
                    continue
                d = b[i + 1:i + 1 + need]
                if hi == 0xB0:
                    yield ("cc", x & 0x0F, d[0], d[1])
                i += 1 + need
            else:
                i += 1
        self.buf = b[i:]

    def handle(self, e) -> None:
        if e[0] == "cc":
            _, ch, num, val = e
            self.progress.log(cc_text(num, val))
            return
        m = e[1]
        raw = m.hex(" ").upper()
        if len(m) >= 7 and m[5] == 0x7C:
            self.pending = m[6]
            what = f"dev requests {NAMES.get(m[6], f'component {m[6]}')}"
        elif len(m) >= 7 and m[5] == 0x01 and m[6] == 0x7D:
            self.saw_hello = True
            self.model = m[4]
            what = "dev bootloader handshake"
        elif len(m) >= 8 and m[5] == 0x7D:
            what = f"DEVICE ERROR {m[6]} (state {m[7]})"
        elif len(m) >= 7 and m[5] == 0x01 and m[6] == 0x02 and self.model is not None:
            wwrite(self.fd, bytes([0xF0, 0x00, 0x21, 0x07, self.model, 0x01, 0x04, 0x01, 0xF7]))
            what = "dev asks to start again; start re-sent"
        else:
            what = f"dev {raw}"
            raw = ""
        self.progress.log(f"{what}   [{raw}]" if self.verbose and raw else what)

    def pump(self, sec: float, stop_on_request: bool = True) -> None:
        end = time.time() + sec
        while time.time() < end:
            if stop_on_request and self.pending is not None:
                return
            r, _, _ = select.select([self.fd], [], [], 0.02)
            self.progress.draw()
            if not r:
                continue
            try:
                for e in self.feeds(os.read(self.fd, 16384)):
                    self.handle(e)
            except OSError:
                return

    def send_image(self, t: int) -> None:
        data = self.images[t]
        self.runs[t] = self.runs.get(t, 0) + 1
        again = f", attempt {self.runs[t]}" if self.runs[t] > 1 else ""
        p = self.progress
        p.log(f"==> sending {NAMES[t]} ({len(data)} msgs @ {self.pace*1000:.0f} ms{again})")
        p.begin(t)
        for i, m in enumerate(data, 1):
            wwrite(self.fd, m)
            time.sleep(self.pace)
            p.advance(i)
        p.log(f"<== {NAMES[t]} sent in {fmt_secs(p.end())}")
        # brief listen; answer any request immediately (retry or next component)
        self.pump(0.30)


def flash(src: str, pace_ms: float = 20.0, total: float = 600.0, verbose: bool = False) -> int:
    import os.path
    images: dict[int, list[bytes]] = {}
    for t, role in NAMES.items():
        fn = next((f for f in os.listdir(src) if f.lower().endswith(".syx")
                   and role in f.lower()), None)
        if not fn:
            sys.exit(f"no {role} .syx in {src}")
        images[t] = [bytes(p) for p in syx.split_packets(open(os.path.join(src, fn), "rb").read())]

    port = find_port()
    if not port:
        sys.exit("no Modal 8-series sound card found; is the synth connected and in UPDATE mode?")
    node = node_for(port)
    print(f"port {port} ({node})")
    fd = os.open(node, os.O_RDWR | os.O_NONBLOCK)
    s = Session(fd, images, pace_ms, verbose)
    try:
        for _ in range(5):                       # identify + confirm update mode
            wwrite(fd, IDENTIFY)
            s.pump(0.5)
            if s.saw_hello:
                break
        if not s.saw_hello:
            sys.exit("device did not answer the update handshake — put it in UPDATE mode "
                     "(power on holding SHIFT/INIT until it says 'waiting for MODALapp "
                     "connection') and retry")
        set_models = {m[4] for img in images.values() for m in img}
        if set_models != {s.model}:
            sys.exit(f"the bootloader reports model {s.model:#04x} but the set in {src} is built "
                     f"for {', '.join(f'{x:#04x}' for x in sorted(set_models))}; refusing to flash. "
                     f"Use the patch file whose target matches this unit.")
        print(f"device is in update mode (model {s.model:#04x}); "
              f"{sum(len(d) for d in images.values())} msgs to send")
        wwrite(fd, bytes([0xF0, 0x00, 0x21, 0x07, s.model, 0x01, 0x04, 0x01, 0xF7]))
        deadline = time.time() + total
        while time.time() < deadline and len(s.served) < len(images):
            s.pump(0.05, stop_on_request=False)
            if s.pending is not None and s.pending in images:
                t = s.pending
                s.pending = None
                s.send_image(t)
                s.served.add(t)
        s.progress.close(keep=len(s.served) < len(images))
        print(f"finished in {fmt_secs(time.time() - s.progress.t0)}. "
              f"served {sorted(s.served)} runs={s.runs}")
        return 0 if len(s.served) == len(images) else 1
    finally:
        s.progress.close()
        os.close(fd)


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):        # progress is visible when piped/logged
        sys.stdout.reconfigure(line_buffering=True)
    ap = argparse.ArgumentParser(description="Flash a patched Modal set over USB-MIDI (Linux/ALSA)")
    ap.add_argument("--dir", required=True)
    ap.add_argument("--pace-ms", type=float, default=20.0,
                    help="ms between SysEx messages (default: %(default)g; do not go below 20)")
    ap.add_argument("--total", type=float, default=600.0)
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="also show the raw bytes of every device message")
    a = ap.parse_args(argv)
    return flash(a.dir, a.pace_ms, a.total, a.verbose)


if __name__ == "__main__":
    raise SystemExit(main())
