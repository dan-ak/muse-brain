"""Turn state into pixels. Pure: no network, no clock, no game rules.

The colour ramp is deliberately not a linear RGB interpolation. Lerping blue to
red passes through a half-brightness purple, so the middle of the scale reads as
a fault rather than as a middle value. Interpolating along the hue circle
instead (240 degrees to 360) keeps every point fully saturated and equally
bright: blue -> violet -> magenta -> red.
"""

from __future__ import annotations

RELAXED = (0, 0, 255)
CONCENTRATED = (255, 0, 0)
OFF = (0, 0, 0)


def focus_to_rgb(drive: float) -> tuple[int, int, int]:
    """-1.0 -> blue (relaxed), 0.0 -> magenta (baseline), +1.0 -> red.

    The drive is signed: zero means "at your own calibrated baseline", not
    "relaxed".

    Written out rather than routed through ``colorsys`` so the endpoints and
    the midpoint land on exact bytes.
    """
    t = (min(1.0, max(-1.0, float(drive))) + 1.0) / 2.0
    if t <= 0.5:
        return (round(255 * t * 2), 0, 255)
    return (255, 0, round(255 * (1 - (t - 0.5) * 2)))


def scale(rgb, brightness: float) -> tuple[int, int, int]:
    """Dim a colour. Brightness is clamped to [0, 1]."""
    factor = min(1.0, max(0.0, float(brightness)))
    return tuple(round(channel * factor) for channel in rgb)


def render_solo(drive: float, count: int) -> list[tuple[int, int, int]]:
    """One wearer's state across the whole strip."""
    return [focus_to_rgb(drive)] * count
