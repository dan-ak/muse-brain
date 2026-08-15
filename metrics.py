"""Named EEG metrics and the calibration that makes them comparable.

Pure maths: no I/O, no clock, no knowledge of seats or strips. A ``bands`` dict
holds the five channel-averaged band powers the phone computes every 250 ms.

Every metric is a log-space value so ratios are symmetric, and every metric
carries a direction, so "low alpha" is simply the negation of "high alpha" and
calibration works identically for both.
"""

from __future__ import annotations

import math

BAND_NAMES = ("delta", "theta", "alpha", "beta", "gamma")

# Matches the epsilon the phone uses, so beta/theta agrees with the score the
# tablet already displays.
_EPS = 1e-6


def is_finite_number(value) -> bool:
    """True for a real, finite number — and notably False for bool and None.

    Lives here, in the module both the server and the light driver already
    depend on, because both have to answer the same question about the same
    numbers: JSON.stringify turns NaN and Infinity into null, and powerByBand
    can produce either, so without this check nulls and strings flow straight
    into numeric CSV columns and into the colour ramp.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return value == value and value not in (float("inf"), float("-inf"))


def _log(value) -> float:
    """log10 with a floor, so a dead channel reading 0 does not give -inf."""
    return math.log10(max(0.0, float(value)) + _EPS)


_RATIOS = {
    "beta_theta": lambda b: _log(b["beta"]) - _log(b["theta"]),
    "alpha_beta": lambda b: _log(b["alpha"]) - _log(b["beta"]),
    "alpha": lambda b: _log(b["alpha"]),
}

# id -> (label for the dropdown, ratio id, direction)
METRICS = {
    "beta_theta_high": ("High beta/theta (focus)", "beta_theta", +1),
    "beta_theta_low": ("Low beta/theta", "beta_theta", -1),
    "alpha_beta_high": ("High alpha/beta", "alpha_beta", +1),
    "alpha_beta_low": ("Low alpha/beta", "alpha_beta", -1),
    "alpha_high": ("High alpha", "alpha", +1),
    "alpha_low": ("Low alpha", "alpha", -1),
}

DEFAULT_METRIC = "beta_theta_high"

# Below this the relax and focus phases did not separate, so the range is noise.
MIN_HALF_RANGE = 0.05
FALLBACK_HALF_RANGE = 1.0

# What an uncalibrated player gets: the raw log ratio, unscaled.
NEUTRAL_CALIBRATION = (0.0, 1.0)


def metric_value(bands, metric_id: str) -> float:
    """The signed value of one metric for one set of band powers."""
    _, ratio_id, sign = METRICS[metric_id]
    return sign * _RATIOS[ratio_id](bands)


def calibration_from(relax_bands, focus_bands, metric_id: str) -> tuple[float, float]:
    """``(baseline, half_range)`` from the two calibration phases.

    Same shape as the tablet's existing calibration: the baseline is the
    midpoint of the two states and the half-range is half their separation, so
    a calibrated drive runs -1 at relax to +1 at focus.
    """
    relax = metric_value(relax_bands, metric_id)
    focus = metric_value(focus_bands, metric_id)
    baseline = 0.5 * (relax + focus)
    half_range = abs(0.5 * (focus - relax))
    if half_range < MIN_HALF_RANGE:
        half_range = FALLBACK_HALF_RANGE
    return baseline, half_range


def calibrate_all(relax_bands, focus_bands) -> dict[str, tuple[float, float]]:
    """Calibrate every metric from one ceremony."""
    return {
        metric_id: calibration_from(relax_bands, focus_bands, metric_id)
        for metric_id in METRICS
    }


def drive(bands, metric_id: str, calibration=None) -> float:
    """A metric as a signed drive in [-1, +1]."""
    baseline, half_range = calibration or NEUTRAL_CALIBRATION
    # ``calibration_from`` never returns a half-range this small, but a
    # calibration that arrives from anywhere else can: a stored one from an
    # older format, or a client that sends its own. A zero would divide by
    # zero and a negative would invert the whole scale, so apply the same
    # floor here rather than trusting the caller. Note ``(0.0, 0.0)`` is a
    # truthy tuple, so the ``or`` above does not catch it.
    if half_range < MIN_HALF_RANGE:
        half_range = FALLBACK_HALF_RANGE
    value = metric_value(bands, metric_id)
    return max(-1.0, min(1.0, (value - baseline) / half_range))
