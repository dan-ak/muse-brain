import math

from neurofeedback import focus_signal_from_bands, ema


def test_focus_signal_is_beta_minus_theta():
    # Higher beta relative to theta => more focused => larger signal.
    assert focus_signal_from_bands(theta_log=0.2, beta_log=0.5) == 0.3


def test_focus_signal_is_nan_when_bands_missing():
    assert math.isnan(focus_signal_from_bands(float("nan"), 0.5))
    assert math.isnan(focus_signal_from_bands(0.2, float("nan")))


def test_ema_seeds_on_first_nan_prev():
    # With no prior value, EMA adopts the new sample.
    assert ema(float("nan"), 3.0, dt=0.1, tau=0.7) == 3.0


def test_ema_zero_tau_passes_through():
    assert ema(1.0, 5.0, dt=0.1, tau=0.0) == 5.0


def test_ema_moves_toward_new_value():
    out = ema(0.0, 1.0, dt=0.7, tau=0.7)  # dt == tau => alpha = 1 - 1/e
    assert math.isclose(out, 1.0 - math.exp(-1.0), rel_tol=1e-9)


from neurofeedback import Calibrator


def test_calibrator_midpoint_and_half_range():
    cal = Calibrator()
    for r in (0.0, 1.0):       # relax mean = 0.5
        cal.add_relax(r)
    for r in (3.0, 5.0):       # focus mean = 4.0
        cal.add_focus(r)
    baseline, half_range, ok = cal.result()
    assert ok is True
    assert baseline == 2.25     # midpoint of 0.5 and 4.0
    assert half_range == 1.75   # half of (4.0 - 0.5)


def test_calibrator_degenerate_when_states_too_close():
    cal = Calibrator()
    cal.add_relax(1.0)
    cal.add_focus(1.0)          # focus == relax => no usable range
    baseline, half_range, ok = cal.result()
    assert ok is False
    assert half_range == 1.0    # safe non-zero fallback


def test_calibrator_needs_both_phases():
    cal = Calibrator()
    cal.add_relax(1.0)
    assert cal.result()[2] is False


def test_calibrator_ignores_nan_samples():
    cal = Calibrator()
    cal.add_relax(float("nan"))
    cal.add_relax(0.0)
    cal.add_focus(4.0)
    baseline, half_range, ok = cal.result()
    assert ok is True
    assert baseline == 2.0      # midpoint of 0.0 and 4.0


from neurofeedback import FocusCursor


def test_cursor_integrates_drive_over_time():
    # tau=0 => smoothed == raw; gain=1, leak=0 => x += drive*dt each step.
    c = FocusCursor(gain=1.0, leak=0.0, tau=0.0, baseline=0.0, half_range=1.0)
    assert c.update(1.0, dt=0.5) == 0.5
    assert c.update(1.0, dt=0.5) == 1.0


def test_cursor_clamps_to_unit_range():
    c = FocusCursor(gain=1.0, leak=0.0, tau=0.0, baseline=0.0, half_range=1.0)
    c.update(1.0, dt=0.5)
    c.update(1.0, dt=0.5)
    assert c.update(1.0, dt=0.5) == 1.0   # would be 1.5, clamped


def test_cursor_drive_is_clamped_unit():
    c = FocusCursor(gain=1.0, leak=0.0, tau=0.0, baseline=0.0, half_range=1.0)
    c.update(10.0, dt=0.1)   # raw far above range
    assert c.drive == 1.0


def test_cursor_leak_pulls_toward_center():
    c = FocusCursor(gain=0.0, leak=1.0, tau=0.0, baseline=0.0, half_range=1.0)
    c.x = 1.0
    c.update(0.0, dt=0.5)    # x += (0 - 1.0*1.0)*0.5
    assert c.x == 0.5


def test_cursor_freezes_on_nan_signal():
    c = FocusCursor(tau=0.0)
    c.x = 0.4
    assert c.update(float("nan"), dt=0.5) == 0.4
    assert c.x == 0.4


def test_cursor_recenter_resets_position():
    c = FocusCursor(tau=0.0)
    c.x = 0.7
    c.recenter()
    assert c.x == 0.0


def test_cursor_set_calibration_guards_zero_range():
    c = FocusCursor()
    c.set_calibration(baseline=2.0, half_range=0.0)
    assert c.baseline == 2.0
    assert c.half_range == 1.0   # zero replaced with safe 1.0
