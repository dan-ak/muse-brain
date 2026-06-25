"""Diagnostic OSC sniffer.

Listens on UDP :5000 and prints a one-line summary of every OSC message that
arrives, plus a periodic per-address rate report. Run for a few seconds with
Mind Monitor streaming, then ctrl-C.
"""

import argparse
import sys
import time
from collections import Counter, defaultdict

from pythonosc import dispatcher, osc_server


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--quiet", action="store_true",
                        help="suppress per-message lines, only print rate summary")
    args = parser.parse_args()

    counts: Counter[str] = Counter()
    last_args: dict[str, tuple] = {}
    start = time.perf_counter()
    last_print = start

    def on_any(addr, *osc_args):
        nonlocal last_print
        counts[addr] += 1
        last_args[addr] = osc_args
        if not args.quiet:
            preview = ", ".join(f"{a:.3f}" if isinstance(a, float) else repr(a)
                                for a in osc_args[:6])
            extra = "" if len(osc_args) <= 6 else f" …(+{len(osc_args)-6})"
            print(f"{addr:32s} ({len(osc_args)} args) {preview}{extra}", flush=True)
        now = time.perf_counter()
        if now - last_print >= 2.0:
            elapsed = now - start
            print(f"\n--- {elapsed:.1f}s totals (rate Hz) ---", file=sys.stderr)
            for a, c in counts.most_common():
                print(f"  {a:32s} {c:>8d}   {c/elapsed:7.1f} Hz",
                      file=sys.stderr)
            print("", file=sys.stderr)
            last_print = now

    disp = dispatcher.Dispatcher()
    disp.set_default_handler(on_any)
    server = osc_server.ThreadingOSCUDPServer((args.host, args.port), disp)
    print(f"Listening on {args.host}:{args.port}. Ctrl-C to stop.", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        elapsed = time.perf_counter() - start
        print(f"\n=== final totals over {elapsed:.1f}s ===", file=sys.stderr)
        for a, c in counts.most_common():
            sample = last_args.get(a, ())
            preview = ", ".join(f"{x:.3f}" if isinstance(x, float) else repr(x)
                                for x in sample[:6])
            print(f"  {a:32s} {c:>8d}   {c/elapsed:7.1f} Hz   last: {preview}",
                  file=sys.stderr)


if __name__ == "__main__":
    main()
