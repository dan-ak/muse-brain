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
