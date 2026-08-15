"""Drive a WLED controller from a seat's focus score.

Maps one seat's ``normalized`` score onto a blue-to-red ramp and streams it to a
WLED controller over its UDP realtime protocol (DNRGB). Pure functions plus a
fire-and-forget socket: no asyncio, no wall clock, and nothing here can raise
into the caller, because the caller is the loop that records the EEG.

The mapping is deliberately not a linear RGB interpolation. Lerping blue to red
passes through a half-brightness purple, so the middle of the scale reads as a
fault rather than as a middle value. Interpolating along the hue circle instead
(240 degrees to 360) keeps every point on the ramp fully saturated and equally
bright: blue -> violet -> magenta -> red.
"""

from __future__ import annotations

import math
import socket
import time

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

RELAXED = (0, 0, 255)
CONCENTRATED = (255, 0, 0)


def focus_to_rgb(normalized: float) -> tuple[int, int, int]:
    """0.0 -> blue (relaxed), 1.0 -> red (concentrated).

    Walks the hue circle from 240 to 360 degrees at full saturation and value,
    which on that arc is exactly two linear segments: blue to magenta, then
    magenta to red. Written out rather than routed through ``colorsys`` so the
    endpoints and the midpoint land on exact bytes.
    """
    t = min(1.0, max(0.0, float(normalized)))
    if t <= 0.5:
        return (round(255 * t * 2), 0, 255)
    return (255, 0, round(255 * (1 - (t - 0.5) * 2)))


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


def build_dnrgb_packets(rgb, count: int, timeout_s: int = DEFAULT_TIMEOUT_S) -> list[bytes]:
    """One solid colour across ``count`` pixels, as WLED DNRGB datagrams."""
    pixel = bytes(rgb)
    packets = []
    start = 0
    while start < count:
        run = min(DNRGB_MAX_PIXELS, count - start)
        header = bytes([DNRGB_PROTOCOL, timeout_s, (start >> 8) & 0xFF, start & 0xFF])
        packets.append(header + pixel * run)
        start += run
    return packets


class WledStrip:
    """A WLED controller addressed over UDP realtime.

    Fire-and-forget by design. Send failures are reported by return value and
    never raised, so an unplugged or dusted-out controller cannot propagate
    into the loop that writes the recording.
    """

    def __init__(self, host: str, count: int, port: int = WLED_REALTIME_PORT,
                 timeout_s: int = DEFAULT_TIMEOUT_S, sock=None):
        self._addr = (host, port)
        self._count = count
        self._timeout_s = timeout_s
        self._sock = sock or socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def show(self, rgb) -> bool:
        """Paint the whole strip. True if every datagram went out."""
        try:
            for packet in build_dnrgb_packets(rgb, self._count, self._timeout_s):
                self._sock.sendto(packet, self._addr)
        except OSError:
            return False
        return True

    def release(self) -> None:
        """Stop driving and let WLED's realtime timeout reclaim the strip.

        Nothing goes on the wire: silence is the signal. WLED returns to its
        own effect once ``timeout_s`` elapses.
        """

    def close(self) -> None:
        self._sock.close()


def _finite(value) -> bool:
    """True for a real, finite number - and notably False for bool and None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return value == value and value not in (float("inf"), float("-inf"))


class FocusLight:
    """Turns hub snapshots into strip colours for one watched seat."""

    def __init__(self, strip, seat: str = "p1", tau_s: float = 1.5, clock_fn=None):
        self._strip = strip
        self._seat = seat
        self._smoother = Smoother(tau_s, clock_fn)

    def update(self, snapshot) -> tuple[int, int, int] | None:
        """Drive one frame. Returns the colour shown, or None if released."""
        score = self._score(snapshot)
        if score is None:
            self._smoother.reset()
            self._strip.release()
            return None
        rgb = focus_to_rgb(self._smoother.update(score))
        self._strip.show(rgb)
        return rgb

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
            return float(value) if _finite(value) else None
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
            for i in range(101):
                strip.show(focus_to_rgb(i / 100.0))
                time.sleep(0.02)
            for i in range(100, -1, -1):
                strip.show(focus_to_rgb(i / 100.0))
                time.sleep(0.02)
    except KeyboardInterrupt:
        return 0
    finally:
        strip.close()


if __name__ == "__main__":
    raise SystemExit(_bring_up())
