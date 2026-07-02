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


class FocusCursor:
    """Velocity-accumulator cursor driven by the (smoothed) focus signal.

    ``update`` EMA-smooths the raw signal, converts it to a drive in [-1, 1]
    using the calibrated baseline/half_range, integrates velocity with a gentle
    self-centering leak, and clamps the position to [-1, 1]. A NaN input freezes
    the cursor (no band data).

    Steady-state position for a *sustained* drive ``d`` is ``(gain/leak)*d`` —
    keep ``gain <= leak`` so the cursor never settles past the drive that's
    actually driving it. A larger ratio lets small persistent biases (e.g. a
    slightly off calibration) creep the whole way to +/-1 given enough dwell
    time, which reads as the cursor being permanently stuck to one side.
    """

    def __init__(self, gain: float = 0.4, leak: float = 0.4, tau: float = 0.7,
                 baseline: float = 0.0, half_range: float = 1.0):
        self.gain = gain          # position units per second at full drive
        self.leak = leak          # self-centering rate (~1/leak seconds)
        self.tau = tau            # EMA smoothing time constant (seconds)
        self.baseline = baseline
        self.half_range = half_range if half_range > 1e-9 else 1.0
        self.x = 0.0              # position in [-1, 1]
        self.smoothed = float("nan")
        self.drive = 0.0

    def set_calibration(self, baseline: float, half_range: float) -> None:
        self.baseline = baseline
        self.half_range = half_range if half_range > 1e-9 else 1.0

    def recenter(self) -> None:
        self.x = 0.0

    def update(self, raw_signal: float, dt: float) -> float:
        if math.isnan(raw_signal):
            return self.x        # freeze: no data
        self.smoothed = ema(self.smoothed, raw_signal, dt, self.tau)
        d = (self.smoothed - self.baseline) / self.half_range
        d = max(-1.0, min(1.0, d))
        self.drive = d
        self.x += (self.gain * d - self.leak * self.x) * dt
        self.x = max(-1.0, min(1.0, self.x))
        return self.x
