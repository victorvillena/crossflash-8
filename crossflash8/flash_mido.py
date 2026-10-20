"""Cross-platform flasher using mido + python-rtmidi (Windows / macOS / Linux).

    pip install mido python-rtmidi
    python -m crossflash8.flash_mido --dir ./out

Same protocol/pacing as the ALSA backend: identify -> start -> serve each component
the bootloader requests, 20 ms per message, answering re-requests immediately.  The
start message uses the model byte the bootloader reports, and a set built for another
model is refused before anything is written.
"""
from __future__ import annotations
import argparse
import os
import sys
import time

from . import syx
from .progress import Progress, cc_text, fmt_secs

IDENTIFY = [0x00, 0x21, 0x07, 0x64, 0x01, 0x01]      # payload without F0 .. F7
NAMES = {0: "panel", 1: "master", 2: "slave"}


def _sysex(msg) -> bytes:
    return bytes([0xF0]) + bytes(msg.data) + bytes([0xF7])


def flash(src: str, pace_ms: float = 20.0, total: float = 600.0, verbose: bool = False) -> int:
    try:
        import mido
    except ImportError:
        sys.exit("mido not installed: pip install mido python-rtmidi")

    images: dict[int, list[bytes]] = {}
    for t, role in NAMES.items():
        fn = next((f for f in os.listdir(src) if f.lower().endswith(".syx")
                   and role in f.lower()), None)
        if not fn:
            sys.exit(f"no {role} .syx in {src}")
        images[t] = [bytes(p) for p in syx.split_packets(open(os.path.join(src, fn), "rb").read())]

    names = [n for n in mido.get_ioport_names()
             if any(w in n.upper() for w in ("CARBON", "COBALT", "ARGON", "MODAL"))]
    if not names:
        sys.exit("no COBALT MIDI port found; is the synth in UPDATE mode?")
    name = names[0]
    print(f"port {name}")

    pace = pace_ms / 1000.0
    progress = Progress({t: len(d) for t, d in images.items()}, NAMES, pace)
    served: set[int] = set()
    runs: dict[int, int] = {}
    pending = None
    saw_hello = False
    model = None

    def handle(msg) -> None:
        nonlocal pending, saw_hello, model
        if msg.type == "control_change":
            progress.log(cc_text(msg.control, msg.value))
            return
        if msg.type != "sysex":
            return
        m = _sysex(msg)
        raw = m.hex(" ").upper()
        if len(m) >= 7 and m[5] == 0x7C:
            pending = m[6]
            what = f"dev requests {NAMES.get(m[6], f'component {m[6]}')}"
        elif len(m) >= 7 and m[5] == 0x01 and m[6] == 0x7D:
            saw_hello = True
            model = m[4]
            what = "dev bootloader handshake"
        elif len(m) >= 8 and m[5] == 0x7D:
            what = f"DEVICE ERROR {m[6]} (state {m[7]})"
        else:
            what = f"dev {raw}"
            raw = ""
        progress.log(f"{what}   [{raw}]" if verbose and raw else what)

    def send_image(port, t: int) -> None:
        data = images[t]
        runs[t] = runs.get(t, 0) + 1
        again = f", attempt {runs[t]}" if runs[t] > 1 else ""
        progress.log(f"==> sending {NAMES[t]} ({len(data)} msgs @ {pace*1000:.0f} ms{again})")
        progress.begin(t)
        for i, pkt in enumerate(data, 1):
            port.send(mido.Message("sysex", data=list(pkt[1:-1])))
            time.sleep(pace)
            progress.advance(i)
        progress.log(f"<== {NAMES[t]} sent in {fmt_secs(progress.end())}")

    with mido.open_ioport(name) as port:
        for _ in range(6):                       # identify + confirm update mode
            port.send(mido.Message("sysex", data=IDENTIFY))
            t_end = time.time() + 0.5
            while time.time() < t_end:
                for msg in port.iter_pending():
                    handle(msg)
                time.sleep(0.02)
            if saw_hello:
                break
        if not saw_hello:
            sys.exit("device did not answer the update handshake — put it in UPDATE mode "
                     "(power on holding SHIFT/INIT until it says 'waiting for MODALapp "
                     "connection') and retry")
        set_models = {m[4] for img in images.values() for m in img}
        if set_models != {model}:
            sys.exit(f"the bootloader reports model {model:#04x} but the set in {src} is built "
                     f"for {', '.join(f'{x:#04x}' for x in sorted(set_models))}; refusing to flash. "
                     f"Use the patch file whose target matches this unit.")
        print(f"device is in update mode (model {model:#04x}); "
              f"{sum(len(d) for d in images.values())} msgs to send")
        port.send(mido.Message("sysex", data=[0x00, 0x21, 0x07, model, 0x01, 0x04, 0x01]))
        deadline = time.time() + total
        try:
            while time.time() < deadline and len(served) < len(images):
                for msg in port.iter_pending():
                    handle(msg)
                if pending is not None and pending in images:
                    t = pending
                    pending = None
                    send_image(port, t)
                    served.add(t)
                progress.draw()
                time.sleep(0.02)
            progress.close(keep=len(served) < len(images))
        finally:
            progress.close()
        print(f"finished in {fmt_secs(time.time() - progress.t0)}. "
              f"served {sorted(served)} runs={runs}")
        return 0 if len(served) == len(images) else 1


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):        # progress is visible when piped/logged
        sys.stdout.reconfigure(line_buffering=True)
    ap = argparse.ArgumentParser(description="Flash a patched Modal set over USB-MIDI (mido)")
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
