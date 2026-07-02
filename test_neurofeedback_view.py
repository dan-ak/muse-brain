import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import time

import pytest
from PyQt5 import QtWidgets

from muse_visualizer import OSCReceiver
from session_recorder import SessionRecorder
from neurofeedback_view import NeurofeedbackView


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _make_view(tmp_path):
    receiver = OSCReceiver(host="127.0.0.1", port=0)   # no thread started
    recorder = SessionRecorder(base_dir=tmp_path)
    view = NeurofeedbackView(receiver, recorder,
                             label_fn=lambda: "nf", subject_fn=lambda: "dan")
    return receiver, recorder, view


def _feed_bands(receiver, theta, beta):
    with receiver.lock:
        receiver.theta_abs = theta
        receiver.beta_abs = beta
        receiver.bands_ts = time.monotonic()


def test_cursor_moves_right_when_focused(app, tmp_path):
    receiver, recorder, view = _make_view(tmp_path)
    try:
        # baseline 0, half_range 1: beta-theta = 1.0 => full positive drive.
        view.cursor.set_calibration(baseline=0.0, half_range=1.0)
        _feed_bands(receiver, theta=0.0, beta=1.0)
        for _ in range(20):
            view._tick()
        assert view.cursor.x > 0.0
    finally:
        receiver.server.server_close()


def test_no_data_keeps_cursor_centered(app, tmp_path):
    receiver, recorder, view = _make_view(tmp_path)
    try:
        for _ in range(10):     # bands_ts == 0 => latest_band_powers is nan/nan
            view._tick()
        assert view.cursor.x == 0.0
    finally:
        receiver.server.server_close()


def test_session_records_raw_and_derived_streams(app, tmp_path):
    receiver, recorder, view = _make_view(tmp_path)
    try:
        _feed_bands(receiver, theta=0.0, beta=1.0)
        view.toggle_session()                 # starts recorder + protocol + logger
        assert recorder.active is True
        session_dir = recorder._session_dir
        for _ in range(5):
            view._tick()                      # writes feedback rows + raw OSC tap
        receiver._on_eeg("/muse/eeg", 1.0, 2.0, 3.0, 4.0)   # raw OSC still captured
        view._stop_session()
        assert recorder.active is False

        assert (session_dir / "feedback.csv").exists()
        assert (session_dir / "eeg.csv").exists()
        meta = json.loads((session_dir / "meta.json").read_text())
        assert meta["mode"] == "neurofeedback"
        assert "protocol" in meta
    finally:
        receiver.server.server_close()


def test_dashboard_hosts_neurofeedback_view_and_toggles(app, tmp_path):
    from muse_visualizer import MuseDashboard

    receiver = OSCReceiver(host="127.0.0.1", port=0)
    try:
        win = MuseDashboard(receiver, port=0, rec_dir=str(tmp_path),
                            label="museA", subject="dan")
        assert hasattr(win, "nf_view")
        assert win.stack.currentWidget() is not win.nf_view
        win._toggle_view()
        assert win.stack.currentWidget() is win.nf_view
        # 'r' record is a no-op while on the neurofeedback page
        win._toggle_record()
        assert win.recorder.active is False
    finally:
        receiver.server.server_close()


def test_calibration_ignores_samples_during_settle_window(app, tmp_path):
    receiver, recorder, view = _make_view(tmp_path)
    try:
        view.start_calibration()
        t0 = view._calib_phase_start
        view._tick_calibration(t0 + 1.0, 5.0)   # within settle window: ignored
        assert len(view._calibrator._relax) == 0
        view._tick_calibration(t0 + 3.5, 7.0)   # past settle window: recorded
        assert view._calibrator._relax == [7.0]
    finally:
        receiver.server.server_close()


def test_dashboard_r_does_not_clobber_running_nf_session(app, tmp_path):
    import time as _time
    from muse_visualizer import MuseDashboard

    receiver = OSCReceiver(host="127.0.0.1", port=0)
    try:
        win = MuseDashboard(receiver, port=0, rec_dir=str(tmp_path), label="museA")
        with receiver.lock:
            receiver.theta_abs, receiver.beta_abs = 0.0, 1.0
            receiver.bands_ts = _time.monotonic()
        win._toggle_view()             # to NF page
        win.nf_view.toggle_session()   # start cued session (starts recorder)
        assert win.recorder.active is True
        win._toggle_view()             # back to dashboard page
        win._toggle_record()           # 'r' must NOT stop the running NF session
        assert win.recorder.active is True
        assert win.nf_view.session is not None
        win.nf_view._stop_session()    # clean up
    finally:
        receiver.server.server_close()
