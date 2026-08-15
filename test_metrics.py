import pytest

from metrics import (
    DEFAULT_METRIC,
    METRICS,
    NEUTRAL_CALIBRATION,
    calibrate_all,
    calibration_from,
    drive,
    metric_value,
)


def bands(delta=1.0, theta=1.0, alpha=1.0, beta=1.0, gamma=1.0):
    return {"delta": delta, "theta": theta, "alpha": alpha, "beta": beta, "gamma": gamma}


def test_default_metric_is_the_existing_focus_score():
    assert DEFAULT_METRIC == "beta_theta_high"


def test_all_six_metrics_are_registered():
    assert set(METRICS) == {
        "beta_theta_high", "beta_theta_low",
        "alpha_beta_high", "alpha_beta_low",
        "alpha_high", "alpha_low",
    }


def test_beta_theta_matches_the_phone_formula():
    # log10(beta) - log10(theta); the phone computes exactly this.
    value = metric_value(bands(beta=100.0, theta=1.0), "beta_theta_high")
    assert value == pytest.approx(2.0, abs=1e-4)


def test_low_direction_is_the_negation_of_high():
    b = bands(beta=100.0, theta=1.0)
    assert metric_value(b, "beta_theta_low") == pytest.approx(
        -metric_value(b, "beta_theta_high"), abs=1e-9)


def test_alpha_beta_is_a_log_ratio():
    value = metric_value(bands(alpha=10.0, beta=1.0), "alpha_beta_high")
    assert value == pytest.approx(1.0, abs=1e-4)


def test_zero_power_does_not_blow_up():
    # powerByBand can return 0 for a dead channel; log10(0) is -inf.
    value = metric_value(bands(alpha=0.0, beta=0.0), "alpha_beta_high")
    assert value == pytest.approx(0.0, abs=1e-9)


def test_calibration_centres_between_relax_and_focus():
    baseline, half_range = calibration_from(
        bands(beta=1.0, theta=1.0), bands(beta=100.0, theta=1.0), "beta_theta_high")
    assert baseline == pytest.approx(1.0, abs=1e-4)
    assert half_range == pytest.approx(1.0, abs=1e-4)


def test_calibrated_drive_spans_minus_one_to_plus_one():
    relax, focus = bands(beta=1.0, theta=1.0), bands(beta=100.0, theta=1.0)
    cal = calibration_from(relax, focus, "beta_theta_high")
    assert drive(relax, "beta_theta_high", cal) == pytest.approx(-1.0, abs=1e-4)
    assert drive(focus, "beta_theta_high", cal) == pytest.approx(1.0, abs=1e-4)


def test_drive_is_clamped_beyond_the_calibrated_range():
    cal = calibration_from(
        bands(beta=1.0, theta=1.0), bands(beta=100.0, theta=1.0), "beta_theta_high")
    assert drive(bands(beta=10_000.0, theta=1.0), "beta_theta_high", cal) == 1.0


def test_degenerate_range_falls_back_rather_than_dividing_by_nothing():
    # A metric that barely moves between the two phases would otherwise divide
    # by almost nothing and pin the drive to +/-1 on noise.
    _, half_range = calibration_from(
        bands(alpha=1.0), bands(alpha=1.0), "alpha_high")
    assert half_range == 1.0


def test_alpha_contrast_runs_backwards_and_still_calibrates():
    # Alpha falls when concentrating, so focusMean < relaxMean. abs() handles
    # the magnitude; "high alpha" is then a game won by relaxing.
    relax, focus = bands(alpha=100.0), bands(alpha=1.0)
    cal = calibration_from(relax, focus, "alpha_high")
    assert drive(relax, "alpha_high", cal) == pytest.approx(1.0, abs=1e-4)
    assert drive(focus, "alpha_high", cal) == pytest.approx(-1.0, abs=1e-4)


def test_calibrate_all_covers_every_metric():
    cals = calibrate_all(bands(beta=1.0, theta=1.0), bands(beta=100.0, theta=1.0))
    assert set(cals) == set(METRICS)
    for baseline, half_range in cals.values():
        assert half_range > 0


def test_uncalibrated_drive_uses_the_raw_ratio():
    assert NEUTRAL_CALIBRATION == (0.0, 1.0)
    value = drive(bands(beta=10.0, theta=1.0), "beta_theta_high")
    assert value == pytest.approx(1.0, abs=1e-4)
