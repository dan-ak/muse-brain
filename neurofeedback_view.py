"""Qt view for the focus/relax neurofeedback demo.

Wires the shared OSCReceiver (band powers) and SessionRecorder (raw capture) to
the pure FocusCursor / CueSession logic, renders the left/right cursor game, and
runs guided calibration and cued-trial recording sessions.
"""

from __future__ import annotations

import math
import time

import pyqtgraph as pg
from PyQt5 import QtCore, QtWidgets

from neurofeedback import Calibrator, FocusCursor, focus_signal_from_bands
from cue_protocol import (CueSession, FOCUS, RELAX, build_sequence)
from trial_logger import TrialLogger

UPDATE_HZ = 30
REST_S = 5.0
CUE_S = 10.0
TARGET = 0.8           # |cursor| past this = inside a target zone
DWELL = 1.0            # seconds in-zone to score a hit
RELAX_SECS = 20.0      # guided-calibration relax phase
FOCUS_SECS = 20.0      # guided-calibration focus phase


class NeurofeedbackView(QtWidgets.QWidget):
    def __init__(self, receiver, recorder, label_fn, subject_fn,
                 n_cues: int = 8, seed: int = 0):
        super().__init__()
        self.receiver = receiver
        self.recorder = recorder
        self._label_fn = label_fn
        self._subject_fn = subject_fn
        self._n_cues = n_cues
        self._seed = seed

        self.cursor = FocusCursor()
        self.session = None        # CueSession while a cued session runs
        self.logger = None
        self._logged = 0
        self._session_t0 = 0.0

        self._calibrator = None
        self._calib_phase = None   # None | "relax" | "focus"
        self._calib_until = 0.0
        self._calib_baseline = self.cursor.baseline
        self._calib_half_range = self.cursor.half_range

        self._last_t = time.monotonic()

        self.setStyleSheet("background-color: #000000; color: #ffffff;")
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        self.cue_label = QtWidgets.QLabel("Free practice — 'c' calibrate · 't' cued session")
        self.cue_label.setAlignment(QtCore.Qt.AlignCenter)
        self.cue_label.setStyleSheet("font: bold 30pt 'sans-serif'; background: transparent;")
        self.cue_label.setMaximumHeight(80)
        layout.addWidget(self.cue_label)

        self.plot = pg.PlotWidget()
        self.plot.setMouseEnabled(False, False)
        self.plot.hideButtons()
        self.plot.hideAxis("left")
        self.plot.hideAxis("bottom")
        self.plot.setXRange(-1.15, 1.15, padding=0)
        self.plot.setYRange(-1.0, 1.0, padding=0)
        layout.addWidget(self.plot, stretch=1)

        # Target zones (left = relax, right = focus) and the center line.
        self.plot.addItem(pg.LinearRegionItem(
            values=[-1.15, -TARGET], orientation="vertical", movable=False,
            brush=pg.mkBrush(60, 120, 220, 60)))
        self.plot.addItem(pg.LinearRegionItem(
            values=[TARGET, 1.15], orientation="vertical", movable=False,
            brush=pg.mkBrush(220, 200, 70, 60)))
        self.plot.addItem(pg.InfiniteLine(pos=0.0, angle=90,
                          pen=pg.mkPen(120, 120, 140, width=1)))
        left_lbl = pg.TextItem("RELAX ◀", color=(150, 190, 255), anchor=(0, 0.5))
        left_lbl.setPos(-1.1, 0.85)
        self.plot.addItem(left_lbl)
        right_lbl = pg.TextItem("▶ FOCUS", color=(255, 230, 130), anchor=(1, 0.5))
        right_lbl.setPos(1.1, 0.85)
        self.plot.addItem(right_lbl)
        self._cursor_item = pg.ScatterPlotItem(
            x=[0.0], y=[0.0], size=42, brush=pg.mkBrush(255, 255, 255),
            pen=pg.mkPen(0, 0, 0))
        self.plot.addItem(self._cursor_item)

        self.status_label = QtWidgets.QLabel("waiting for signal…")
        self.status_label.setStyleSheet("font: 12pt 'monospace'; background: transparent;")
        self.status_label.setMaximumHeight(28)
        layout.addWidget(self.status_label)

        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(int(1000 / UPDATE_HZ))

    # ---- public hotkey actions -------------------------------------------

    def recenter(self):
        self.cursor.recenter()
        self._render_cursor()

    def start_calibration(self):
        if self.session is not None:
            return
        self._calibrator = Calibrator()
        self._calib_phase = "relax"
        self._calib_until = time.monotonic() + RELAX_SECS

    def toggle_session(self):
        if self.session is not None:
            self._stop_session()
            return
        if self._calib_phase is not None:
            return
        if self.recorder.active:
            self.status_label.setText("Recorder busy — stop dashboard recording first")
            return
        extra = {
            "mode": "neurofeedback",
            "baseline": self._calib_baseline,
            "half_range": self._calib_half_range,
            "protocol": {"n_cues": self._n_cues, "seed": self._seed,
                         "rest_s": REST_S, "cue_s": CUE_S,
                         "target": TARGET, "dwell_s": DWELL},
        }
        try:
            session_dir = self.recorder.start(self._label_fn(), self._subject_fn(),
                                              extra_meta=extra)
        except OSError as exc:
            self.status_label.setText(f"Session failed: {exc}")
            return
        self.receiver.recorder = self.recorder
        self.logger = TrialLogger(session_dir)
        self.session = CueSession(build_sequence(self._n_cues, self._seed, REST_S, CUE_S),
                                  clock_fn=time.monotonic, target=TARGET, dwell_needed=DWELL)
        self._logged = 0
        self._session_t0 = time.monotonic()
        self.cursor.recenter()

    def shutdown(self):
        """Flush + close the logger if a session is open (window-close path)."""
        try:
            self._flush_new_results()
        except Exception:
            pass
        if self.logger is not None:
            self.logger.close()
            self.logger = None

    # ---- per-frame loop ---------------------------------------------------

    def _tick(self):
        now = time.monotonic()
        dt = now - self._last_t
        self._last_t = now
        if dt <= 0 or dt > 1.0:
            dt = 1.0 / UPDATE_HZ

        theta_log, beta_log = self.receiver.latest_band_powers()
        raw = focus_signal_from_bands(theta_log, beta_log)

        if self._calib_phase is not None:
            self._tick_calibration(now, raw)
            self._render_cursor()
            return

        x = self.cursor.update(raw, dt)
        self._render_cursor()

        if self.session is not None:
            state = self.session.update(x)
            self._flush_new_results()
            active = state.phase if state.phase in (FOCUS, RELAX) else ""
            if self.logger is not None:
                self.logger.log_feedback(now - self._session_t0, raw,
                                         self.cursor.smoothed, self.cursor.drive,
                                         x, active)
            self._render_session(state)
            if state.done:
                self._stop_session()
        else:
            self._render_idle(raw)

    def _tick_calibration(self, now, raw):
        remaining = max(0.0, self._calib_until - now)
        secs = int(math.ceil(remaining))
        if self._calib_phase == "relax":
            self._calibrator.add_relax(raw)
            self.cue_label.setText(f"RELAX…  {secs}s")
            if remaining <= 0.0:
                self._calib_phase = "focus"
                self._calib_until = now + FOCUS_SECS
        elif self._calib_phase == "focus":
            self._calibrator.add_focus(raw)
            self.cue_label.setText(f"FOCUS…  {secs}s")
            if remaining <= 0.0:
                baseline, half_range, ok = self._calibrator.result()
                if ok:
                    self.cursor.set_calibration(baseline, half_range)
                    self._calib_baseline = baseline
                    self._calib_half_range = half_range
                    self.status_label.setText(
                        f"calibrated · baseline={baseline:.2f} half_range={half_range:.2f}")
                else:
                    self.status_label.setText("weak calibration (focus ≈ relax) — press 'c' to retry")
                self._calib_phase = None
                self.cursor.recenter()

    def _flush_new_results(self):
        if self.session is None or self.logger is None:
            return
        while self._logged < len(self.session.results):
            r = self.session.results[self._logged]
            self.logger.log_trial(r.t_start, r.t_end, r.cue, r.goal_side, r.hit, r.dwell_s)
            self._logged += 1

    def _stop_session(self):
        self._flush_new_results()
        if self.logger is not None:
            self.logger.close()
            self.logger = None
        hits = sum(1 for r in (self.session.results if self.session else []) if r.hit)
        total = len(self.session.results) if self.session else 0
        session_dir = self.recorder.stop()
        self.session = None
        self.cue_label.setText("Free practice — 'c' calibrate · 't' cued session")
        if session_dir is not None:
            self.status_label.setText(f"saved cued session ({hits}/{total} hits) → {session_dir}")

    # ---- rendering --------------------------------------------------------

    def _render_cursor(self):
        self._cursor_item.setData(x=[self.cursor.x], y=[0.0])

    def _render_session(self, state):
        if state.phase == FOCUS:
            self.cue_label.setText(f"FOCUS ▶  ({int(state.time_remaining)}s)  "
                                   f"trial {state.cue_number}/{self._n_cues}")
        elif state.phase == RELAX:
            self.cue_label.setText(f"◀ RELAX  ({int(state.time_remaining)}s)  "
                                   f"trial {state.cue_number}/{self._n_cues}")
        else:
            self.cue_label.setText(f"REST…  ({int(state.time_remaining)}s)")

    def _render_idle(self, raw):
        if math.isnan(raw):
            self.status_label.setText("waiting for signal…")
        else:
            self.status_label.setText(
                f"practice · signal={raw:+.2f} drive={self.cursor.drive:+.2f} "
                f"x={self.cursor.x:+.2f}")
