"""Pure neurofeedback logic: focus signal, calibration, and cursor dynamics.

No Qt, no sockets, no wall clock — dt is passed in by the caller — so every
piece here is deterministic and unit-testable.
"""

from __future__ import annotations

import math


def focus_signal_from_bands(theta_log: float, beta_log: float) -> float:
    """Focus signal from Mind Monitor's log10 band powers.

    Returns ``beta_log - theta_log`` (higher = more focused). Returns NaN if
    either input is NaN, i.e. no band data has arrived yet.
    """
    if math.isnan(theta_log) or math.isnan(beta_log):
        return float("nan")
    return beta_log - theta_log


def ema(prev: float, new: float, dt: float, tau: float) -> float:
    """One exponential-moving-average step with time constant ``tau`` seconds.

    A NaN ``prev`` (no history yet) or non-positive ``tau`` adopts ``new``.
    """
    if math.isnan(prev) or tau <= 0.0:
        return new
    alpha = 1.0 - math.exp(-dt / tau)
    return prev + alpha * (new - prev)


class Calibrator:
    """Collects relax-phase and focus-phase focus-signal samples and derives the
    neutral baseline and half-range used to normalize the cursor drive."""

    def __init__(self):
        self._relax: list[float] = []
        self._focus: list[float] = []

    def add_relax(self, r: float) -> None:
        if not math.isnan(r):
            self._relax.append(r)

    def add_focus(self, r: float) -> None:
        if not math.isnan(r):
            self._focus.append(r)

    def result(self, min_range: float = 1e-3) -> tuple[float, float, bool]:
        """Return ``(baseline, half_range, ok)``.

        ``baseline`` is the midpoint of the relax and focus means; ``half_range``
        is half their difference. ``ok`` is False when a phase has no samples or
        the two states are too close to tell apart (degenerate calibration), in
        which case ``half_range`` is a safe 1.0.
        """
        if not self._relax or not self._focus:
            return 0.0, 1.0, False
        relax_mean = sum(self._relax) / len(self._relax)
        focus_mean = sum(self._focus) / len(self._focus)
        baseline = 0.5 * (relax_mean + focus_mean)
        half_range = 0.5 * (focus_mean - relax_mean)
        if half_range < min_range:
            return baseline, 1.0, False
        return baseline, half_range, True
