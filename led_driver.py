"""Stream pixels to a WLED controller, and drive them from one seat.

Transport — framing a pixel list as WLED DNRGB datagrams and getting them onto
the wire — plus ``FocusLight``, the single-seat driver that predates the game
modes. Colour and layout live in ``strip_render``; when ``solo`` becomes a mode
alongside the battle modes, ``FocusLight`` and ``Smoother`` move there too and
this module is transport alone.

Nothing here can raise into the caller, because the caller is the loop that
records the EEG.
"""

from __future__ import annotations

import math
import socket
import time

from metrics import is_finite_number
from strip_render import CONCENTRATED, RELAXED, focus_to_rgb, render_solo

WLED_REALTIME_PORT = 21324

# WLED UDP realtime protocol 4 (DNRGB): a 4-byte header carrying the protocol
# id, the realtime timeout, and a 16-bit start index, then raw RGB triplets.
# Not 3 - that is DRGBW, which reads the same bytes as RGBW quadruplets and
# renders garbage on an RGB strip without erroring.
DNRGB_PROTOCOL = 4

# 1472 bytes is the usable payload of a 1500-byte MTU datagram; minus the
# 4-byte header that leaves 489 whole pixels per packet.
DNRGB_MAX_PIXELS = 489

# Seconds WLED waits after the last packet before dropping back to its own
# effect. Long enough to bridge a missed frame at 10 Hz, short enough that a
# dead Pi visibly hands the strip back rather than freezing it.
DEFAULT_TIMEOUT_S = 2


class Smoother:
    """Time-aware exponential moving average.

    The raw score arrives at 10 Hz and is jittery enough that a direct mapping
    visibly strobes, which is both ugly and a real concern in a dark space at
    night. Uses elapsed time rather than a per-sample constant so a dropped
    frame eases correctly instead of stalling the ramp.
    """

    def __init__(self, tau_s: float = 1.5, clock_fn=None):
        self._tau = tau_s
        self._clock = clock_fn or time.monotonic
        self._value: float | None = None
        self._last_t: float | None = None

    def update(self, value: float) -> float:
        now = self._clock()
        if self._value is None:
            self._value = float(value)
        else:
            dt = max(0.0, now - self._last_t)
            alpha = 1.0 - math.exp(-dt / self._tau) if self._tau > 0 else 1.0
            self._value += alpha * (float(value) - self._value)
        self._last_t = now
        return round(self._value, 6)

    def reset(self) -> None:
        """Forget the running value so the next sample is taken as-is.

        Called whenever the strip is released: after a dropout the wearer's
        state is unrelated to whatever it was before, so easing from the old
        colour would show a state nobody is in.
        """
        self._value = None
        self._last_t = None


def solid(rgb, count: int) -> list[tuple[int, int, int]]:
    """One colour repeated — the whole-strip case of a pixel list."""
    return [tuple(rgb)] * count


def build_dnrgb_packets(pixels, timeout_s: int = DEFAULT_TIMEOUT_S) -> list[bytes]:
    """A pixel list as WLED DNRGB datagrams, split at the per-packet limit."""
    packets = []
    start = 0
    total = len(pixels)
    while start < total:
        run = min(DNRGB_MAX_PIXELS, total - start)
        header = bytes([DNRGB_PROTOCOL, timeout_s, (start >> 8) & 0xFF, start & 0xFF])
        body = bytearray()
        for r, g, b in pixels[start:start + run]:
            body += bytes((r, g, b))
        packets.append(header + bytes(body))
        start += run
    return packets


def _resolve(host: str) -> str:
    """Host to a literal address, or the host unchanged if it will not resolve."""
    try:
        return socket.gethostbyname(host)
    except OSError:
        return host


class WledStrip:
    """A WLED controller addressed over UDP realtime.

    Fire-and-forget by design. Send failures are reported by return value and
    never raised, so an unplugged or dusted-out controller cannot propagate
    into the loop that writes the recording.
    """

    def __init__(self, host: str, count: int, port: int = WLED_REALTIME_PORT,
                 timeout_s: int = DEFAULT_TIMEOUT_S, sock=None):
        # Resolved once, here, rather than on every ``sendto``. The caller is
        # an asyncio loop that also flushes the recording, and a name like
        # ``wled.local`` would otherwise put a synchronous mDNS lookup in front
        # of every frame - hundreds of milliseconds of stalled loop at 10 Hz
        # whenever the responder is slow or gone. A failure here is not fatal:
        # keep the name and let each send fail cheaply instead.
        self._addr = (_resolve(host), port)
        self._count = count
        self._timeout_s = timeout_s
        if sock is None:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            # A full send buffer must drop the frame, never park the loop.
            sock.setblocking(False)
        self._sock = sock

    def show_pixels(self, pixels) -> bool:
        """Paint an explicit pixel list. True if every datagram went out.

        A failed datagram does not abandon the rest: on a strip long enough to
        need several, stopping early would leave its head on the new colour and
        its tail on the old one.

        Framing is inside the guard too. ``bytes()`` rejects a channel that is
        not a whole number in 0-255, and a renderer that hands over a float or
        an out-of-range value must dim the strip, not raise into the loop that
        writes the recording.
        """
        try:
            packets = build_dnrgb_packets(pixels, self._timeout_s)
        except (TypeError, ValueError):
            return False

        sent = True
        for packet in packets:
            try:
                self._sock.sendto(packet, self._addr)
            except OSError:
                sent = False
        return sent

    def show(self, rgb) -> bool:
        """Paint the whole strip one colour."""
        return self.show_pixels(solid(rgb, self._count))

    def release(self) -> None:
        """Stop driving and let WLED's realtime timeout reclaim the strip.

        Nothing goes on the wire: silence is the signal. WLED returns to its
        own effect once ``timeout_s`` elapses.
        """

    def close(self) -> None:
        self._sock.close()


class FocusLight:
    """Turns hub snapshots into strip pixels for one watched seat."""

    def __init__(self, strip, count: int, seat: str = "p1", tau_s: float = 1.5,
                 clock_fn=None):
        self._strip = strip
        self._seat = seat
        self._count = count
        self._smoother = Smoother(tau_s, clock_fn)

    def update(self, snapshot):
        """Drive one frame. Returns the pixels painted, or None if released."""
        score = self._score(snapshot)
        if score is None:
            self._smoother.reset()
            self._strip.release()
            return None
        pixels = render_solo(self._smoother.update(score), self._count)
        self._strip.show_pixels(pixels)
        return pixels

    def _score(self, snapshot) -> float | None:
        """The watched seat's score, or None if it should not be trusted.

        Disconnected, stale and calibrating all collapse to the same answer:
        there is no meaningful state to show, so hand the strip back rather
        than inventing a neutral colour that looks like a real reading.
        """
        for seat in snapshot.get("seats", ()):
            if seat.get("id") != self._seat:
                continue
            if not seat.get("connected") or seat.get("stale") or seat.get("calibrating"):
                return None
            value = seat.get("normalized")
            return float(value) if is_finite_number(value) else None
        return None


def _bring_up(argv=None):
    """Manual smoke test: prove the strip works before any EEG is involved.

    Not unit-tested, because what it checks is whether photons came out of the
    right end of a wire. ``--check`` exists for one specific failure: WS2815 is
    a GRB part, and if WLED's colour order is left on RGB the strip shows the
    exact opposite of the wearer's state with nothing logging an error.
    """
    import argparse

    parser = argparse.ArgumentParser(description="Smoke-test a WLED strip.")
    parser.add_argument("host", help="WLED controller address")
    parser.add_argument("--count", type=int, default=144)
    parser.add_argument("--check", action="store_true",
                        help="hold blue then red so you can verify colour order")
    args = parser.parse_args(argv)

    # Same guard the server applies to --led-count: a count below 1 produces no
    # datagrams at all, so every send "succeeds" against a strip that never
    # lights. That is exactly the silent failure this tool exists to catch.
    if args.count < 1:
        parser.error(f"--count must be at least 1, got {args.count}")

    strip = WledStrip(args.host, count=args.count)
    try:
        if args.check:
            for name, rgb in (("BLUE (relaxed)", RELAXED), ("RED (concentrated)", CONCENTRATED)):
                print(f"sending {name} - if the strip disagrees, fix the colour order in WLED")
                for _ in range(30):
                    if not strip.show(rgb):
                        print(f"  send failed - is {args.host} reachable?")
                        return 1
                    time.sleep(0.1)
            return 0

        print(f"sweeping blue -> red on {args.host}, ctrl-c to stop")
        while True:
            for i in range(-100, 101):
                strip.show(focus_to_rgb(i / 100.0))
                time.sleep(0.02)
            for i in range(100, -101, -1):
                strip.show(focus_to_rgb(i / 100.0))
                time.sleep(0.02)
    except KeyboardInterrupt:
        return 0
    finally:
        strip.close()


if __name__ == "__main__":
    raise SystemExit(_bring_up())
