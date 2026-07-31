"""Muse 2 EEG visualizer.

Receives OSC from the Mind Monitor phone app on UDP :5000, band-pass filters
the EEG into Delta/Theta/Alpha/Beta/Gamma, and renders five stacked scrolling
waveforms plus a 2D head-orientation crosshair driven by the gyro.
"""

from __future__ import annotations

import argparse
import math
import os
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass

import numpy as np
import trimesh
import pyqtgraph as pg
from PyQt5 import QtCore, QtGui, QtWidgets
from pythonosc import dispatcher, osc_server
from scipy.signal import butter, sosfilt, sosfilt_zi

from session_recorder import SessionRecorder
from neurofeedback_view import NeurofeedbackView

SAMPLE_RATE = 256                       # Muse 2 EEG sample rate
WINDOW_SEC = 5
BUFFER_SIZE = SAMPLE_RATE * WINDOW_SEC  # samples shown per band
UPDATE_HZ = 30
RANGE_WINDOW_SEC = 30                   # rolling window for y-axis extremes
RANGE_CONTRACT_ALPHA = 0.05             # smoothing when bound drifts inward
MIN_HALF_RANGE = 0.5                    # µV — keep plot from collapsing flat
GYRO_SCALE = 0.5                        # dampen exaggerated head movement
POWER_WINDOW_SEC = 10                   # time-average window for band-power → TBR
# TBR gauge: numeric range 0..5, but the visual 50% point is TBR_GAUGE_REF
# (i.e. 0..REF maps to the bottom half, REF..MAX to the top half). The pixel-
# per-ratio resolution is therefore different above vs. below the reference.
TBR_GAUGE_MIN = 0.0
TBR_GAUGE_MAX = 5.0
TBR_GAUGE_REF = 2.0
# Plot's internal display y is in [0, 5]; the reference draws at the midpoint
# (2.5). Helper below maps a real TBR value into that display space.
_TBR_DISPLAY_MID = 0.5 * (TBR_GAUGE_MAX - TBR_GAUGE_MIN)


def tbr_to_display(tbr: float) -> float:
    """Map a real TBR value to display y, where the visual midpoint = TBR_GAUGE_REF."""
    tbr = max(TBR_GAUGE_MIN, min(TBR_GAUGE_MAX, tbr))
    if tbr <= TBR_GAUGE_REF:
        # 0..REF spans the lower half of the display [0, MID]
        span = TBR_GAUGE_REF - TBR_GAUGE_MIN
        return TBR_GAUGE_MIN + (tbr - TBR_GAUGE_MIN) * (_TBR_DISPLAY_MID / span)
    # REF..MAX spans the upper half [MID, MAX]
    span = TBR_GAUGE_MAX - TBR_GAUGE_REF
    return _TBR_DISPLAY_MID + (tbr - TBR_GAUGE_REF) * (_TBR_DISPLAY_MID / span)

BANDS = {
    "Delta (0.5-4 Hz)":  (0.5, 4.0),
    "Theta (4-8 Hz)":    (4.0, 8.0),
    "Alpha (8-13 Hz)":   (8.0, 13.0),
    "Beta  (13-30 Hz)":  (13.0, 30.0),
    "Gamma (30-50 Hz)":  (30.0, 50.0),
}
BAND_COLORS = {
    "Delta (0.5-4 Hz)":  (100, 149, 237),
    "Theta (4-8 Hz)":    (147, 112, 219),
    "Alpha (8-13 Hz)":   (60, 200, 120),
    "Beta  (13-30 Hz)":  (255, 165, 0),
    "Gamma (30-50 Hz)":  (220, 70, 90),
}


DEPTH_BANDS = 6                         # opacity bands for depth-shaded rendering


FACE_OBJ_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "assets", "canonical_face_model.obj")


def polylines_to_segments(polylines: list[np.ndarray]) -> tuple[np.ndarray, np.ndarray]:
    """Flatten a list of polylines into start/end-point arrays for line segments."""
    starts = [p[:-1] for p in polylines]
    ends = [p[1:] for p in polylines]
    return np.vstack(starts), np.vstack(ends)


def _back_of_head_polylines(face_z_min, x_max, y_max, depth=0.65,
                            top_overshoot=0.10):
    """Latitude + longitude arcs for the cranium / back hemisphere.

    Generates a half-ellipsoid extending in -z from the face mesh's ear plane
    (z = face_z_min) backward by `depth`, capped at y = y_max + top_overshoot
    so it covers the crown above the face mesh's hairline.
    """
    polylines = []
    sy = y_max + top_overshoot
    sx = x_max

    # Latitude arcs: at each y, half-ellipse in the x-z plane wrapping around
    # the back of the skull.
    for ny in np.linspace(-0.96, 0.96, 14):
        r = math.sqrt(1 - ny * ny)
        t = np.linspace(0, 1, 36)
        polylines.append(np.column_stack([
            -sx * r + 2 * sx * r * t,
            sy * ny * np.ones_like(t),
            face_z_min - depth * r * np.sin(np.pi * t),
        ]))

    # Longitude arcs: at each x slice, half-ellipse in y-z from top → back → bottom.
    for x_norm in np.linspace(-0.95, 0.95, 11):
        r = math.sqrt(1 - x_norm * x_norm)
        t = np.linspace(0, np.pi, 40)
        polylines.append(np.column_stack([
            sx * x_norm * np.ones_like(t),
            sy * r * np.cos(t),
            face_z_min - depth * r * np.sin(t),
        ]))

    return polylines


def build_head_geometry(obj_path=FACE_OBJ_PATH):
    """Load the canonical face mesh and add a back-of-head wireframe behind it.

    Returns (face_starts, face_ends, back_starts, back_ends): pairs of (N, 3)
    arrays of line-segment endpoints in the local head coord system
    (+x right, +y up, +z out of face). Total head height ≈ 2.0.
    """
    mesh = trimesh.load(obj_path, process=False, force="mesh")
    verts = np.asarray(mesh.vertices, dtype=np.float64).copy()
    edges = np.asarray(mesh.edges_unique, dtype=np.int64)

    # Center the mesh vertically (y) and normalize so the face occupies most
    # of the head height with room left for the cranium.
    y_mid = 0.5 * (verts[:, 1].max() + verts[:, 1].min())
    verts[:, 1] -= y_mid
    y_extent = verts[:, 1].max() - verts[:, 1].min()
    scale = 1.85 / y_extent
    verts *= scale

    face_starts = verts[edges[:, 0]]
    face_ends = verts[edges[:, 1]]

    x_max = float(verts[:, 0].max())
    y_max = float(verts[:, 1].max())
    face_z_min = float(verts[:, 2].min())
    back_polylines = _back_of_head_polylines(face_z_min, x_max, y_max)
    back_starts, back_ends = polylines_to_segments(back_polylines)

    return face_starts, face_ends, back_starts, back_ends


@dataclass
class HeadStyle:
    """Geometry for one head visualization style.

    Two layers so the existing depth-banded pen palette (cool back / warm
    feature) keeps working unchanged across styles.
    """
    name: str
    warm_starts: np.ndarray
    warm_ends: np.ndarray
    cool_starts: np.ndarray
    cool_ends: np.ndarray


def _build_realface_style() -> HeadStyle:
    fs, fe, bs, be = build_head_geometry()
    return HeadStyle(name="Real face (MediaPipe)",
                     warm_starts=fs, warm_ends=fe,
                     cool_starts=bs, cool_ends=be)


def _load_full_obj_as_style(filename: str, name: str,
                            target_max_extent: float = 1.75,
                            flip_to_face_camera: bool = True,
                            swap_yz: bool = False) -> HeadStyle:
    """Load any OBJ as a head style.

    The whole mesh becomes the "warm" layer (front/feature pen). Cool back
    layer is empty since these are full 3D objects. Mesh is centered on its
    bounding-box center and scaled so its largest axis equals
    `target_max_extent` — keeps wide ears (Suzanne) or tall headdresses
    (Nefertiti) inside the plot view.

    `flip_to_face_camera`: rotate 180° around y to convert from Blender's
    -z-forward export convention to our +z-forward convention.
    `swap_yz`: swap y and z axes (for z-up source models).
    """
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)
    mesh = trimesh.load(path, process=False, force="mesh")
    verts = np.asarray(mesh.vertices, dtype=np.float64).copy()
    edges = np.asarray(mesh.edges_unique, dtype=np.int64)

    if swap_yz:
        verts = verts[:, [0, 2, 1]].copy()
    if flip_to_face_camera:
        verts[:, 0] *= -1
        verts[:, 2] *= -1

    bb_center = 0.5 * (verts.max(axis=0) + verts.min(axis=0))
    verts -= bb_center
    extents = verts.max(axis=0) - verts.min(axis=0)
    verts *= target_max_extent / float(extents.max())

    starts = verts[edges[:, 0]]
    ends = verts[edges[:, 1]]
    empty = np.zeros((0, 3), dtype=np.float64)
    return HeadStyle(name=name,
                     warm_starts=starts, warm_ends=ends,
                     cool_starts=empty, cool_ends=empty)


HEAD_STYLES: list[HeadStyle] = [
    _build_realface_style(),
    _load_full_obj_as_style("assets/nefertiti.obj", "Nefertiti bust"),
    _load_full_obj_as_style("assets/suzanne.obj", "Suzanne (Blender)"),
    _load_full_obj_as_style("assets/spot.obj", "Spot the cow"),
]


class OrientationTracker:
    """Maps incoming /muse/gyro (treated as absolute degrees) to a 3x3 rotation matrix.

    Axis mapping: gyro[0]=pitch, gyro[1]=roll, gyro[2]=yaw. Adjust signs/order
    here if the head appears to move along the wrong axis.
    """

    def __init__(self):
        self.angles = np.zeros(3, dtype=np.float64)  # pitch, yaw, roll (radians)

    def set_from_gyro(self, gyro_deg: np.ndarray):
        # Mind Monitor gyro layout for Muse 2: gyro[0]=roll, gyro[1]=pitch, gyro[2]=yaw.
        # Sign conventions: +pitch = nose-up, +yaw = nose-right, +roll = right-ear-down.
        # Flip the sign on a line if the head still moves the wrong way for your headset.
        scaled = gyro_deg * GYRO_SCALE
        self.angles[0] = -math.radians(scaled[1])  # pitch
        self.angles[1] =  math.radians(scaled[2])  # yaw
        self.angles[2] = -math.radians(scaled[0])  # roll

    def matrix(self) -> np.ndarray:
        p, y, r = self.angles
        cp, sp = math.cos(p), math.sin(p)
        cy, sy = math.cos(y), math.sin(y)
        cr, sr = math.cos(r), math.sin(r)
        Rx = np.array([[1, 0, 0], [0, cp, -sp], [0, sp, cp]])
        Ry = np.array([[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]])
        Rz = np.array([[cr, -sr, 0], [sr, cr, 0], [0, 0, 1]])
        return Ry @ Rx @ Rz


class FilterBank:
    """Stateful streaming band-pass filter bank using SOS form."""

    def __init__(self, fs: int = SAMPLE_RATE, order: int = 4):
        self.sos = {}
        self.zi0 = {}      # unscaled steady-state response to unit step
        self.zi = {}       # current filter state (set on first sample)
        self.primed = False
        for name, (lo, hi) in BANDS.items():
            sos = butter(order, [lo, hi], btype="band", fs=fs, output="sos")
            self.sos[name] = sos
            self.zi0[name] = sosfilt_zi(sos)
            self.zi[name] = self.zi0[name].copy()

    def process(self, chunk: np.ndarray) -> dict[str, np.ndarray]:
        if not self.primed and chunk.size:
            # Scale initial state by the first sample so the filter starts at the
            # raw signal's DC level instead of zero — kills the startup transient.
            for name in self.sos:
                self.zi[name] = self.zi0[name] * float(chunk[0])
            self.primed = True
        out = {}
        for name, sos in self.sos.items():
            y, self.zi[name] = sosfilt(sos, chunk, zi=self.zi[name])
            out[name] = y
        return out


class OSCReceiver:
    """Background OSC server. Pushes EEG samples to a deque and stores latest gyro."""

    def __init__(self, host: str = "0.0.0.0", port: int = 5000, buf_max: int = SAMPLE_RATE * 30):
        self.eeg_buffer: deque[float] = deque(maxlen=buf_max)
        self.gyro = np.zeros(3, dtype=np.float64)  # x, y, z
        self.recorder = None  # set to a SessionRecorder to capture raw OSC
        self.lock = threading.Lock()
        self.eeg_count = 0
        # Mind Monitor's pre-computed absolute band powers (log10), 4 channels
        # averaged. Used to compute TBR matching the Muse SDK convention.
        self.theta_abs: float = float("nan")
        self.beta_abs:  float = float("nan")
        self.bands_ts: float = 0.0

        disp = dispatcher.Dispatcher()
        disp.map("/muse/eeg", self._on_eeg)
        disp.map("/muse/gyro", self._on_gyro)
        disp.map("/muse/elements/theta_absolute", self._on_theta_abs)
        disp.map("/muse/elements/beta_absolute",  self._on_beta_abs)
        disp.set_default_handler(self._on_default)

        self.server = osc_server.ThreadingOSCUDPServer((host, port), disp)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def _tap(self, addr, args):
        rec = self.recorder
        if rec is not None and rec.active:
            rec.record(addr, args)

    def _on_eeg(self, addr, *args):
        self._tap(addr, args)
        # Muse 2 sends 4 channels: TP9, AF7, AF8, TP10. Average for a single trace.
        if not args:
            return
        try:
            sample = float(np.mean(args))
        except (TypeError, ValueError):
            return
        with self.lock:
            self.eeg_buffer.append(sample)
            self.eeg_count += 1

    def _on_gyro(self, addr, *args):
        self._tap(addr, args)
        if len(args) < 3:
            return
        with self.lock:
            self.gyro[:] = [float(args[0]), float(args[1]), float(args[2])]

    def _on_theta_abs(self, addr, *args):
        self._tap(addr, args)
        if not args:
            return
        try:
            vals = [float(a) for a in args if a is not None]
        except (TypeError, ValueError):
            return
        vals = [v for v in vals if not math.isnan(v) and not math.isinf(v)]
        if not vals:
            return
        with self.lock:
            self.theta_abs = float(np.mean(vals))
            self.bands_ts = time.monotonic()

    def _on_beta_abs(self, addr, *args):
        self._tap(addr, args)
        if not args:
            return
        try:
            vals = [float(a) for a in args if a is not None]
        except (TypeError, ValueError):
            return
        vals = [v for v in vals if not math.isnan(v) and not math.isinf(v)]
        if not vals:
            return
        with self.lock:
            self.beta_abs = float(np.mean(vals))
            self.bands_ts = time.monotonic()

    def _on_default(self, addr, *args):
        self._tap(addr, args)

    def drain_eeg(self) -> np.ndarray:
        with self.lock:
            if not self.eeg_buffer:
                return np.empty(0, dtype=np.float64)
            data = np.fromiter(self.eeg_buffer, dtype=np.float64)
            self.eeg_buffer.clear()
            return data

    def latest_gyro(self) -> np.ndarray:
        with self.lock:
            return self.gyro.copy()

    def latest_band_powers(self) -> tuple[float, float]:
        """Return (theta_log, beta_log) from Mind Monitor's absolute bands.

        nan/nan if not received in the last 5 s.
        """
        with self.lock:
            if self.bands_ts == 0.0 or time.monotonic() - self.bands_ts > 5.0:
                return float("nan"), float("nan")
            return self.theta_abs, self.beta_abs

    def start(self):
        self.thread.start()

    def stop(self):
        self.server.shutdown()


class MuseDashboard(QtWidgets.QMainWindow):
    def __init__(self, receiver: OSCReceiver, port: int,
                 rec_dir: str = "recordings", label: str = "muse", subject: str = "",
                 nf_cues: int = 8, nf_seed: int = 0):
        super().__init__()
        self.receiver = receiver
        self.filters = FilterBank()
        self.recorder = SessionRecorder(base_dir=rec_dir)
        self.receiver.recorder = self.recorder
        self._default_label = label
        self._default_subject = subject
        self._nf_cues = nf_cues
        self._nf_seed = nf_seed
        self._rec_start_mono = 0.0
        self._rec_start_count = 0
        self.display = {name: np.zeros(BUFFER_SIZE, dtype=np.float64) for name in BANDS}

        # Theta/Beta ratio: rolling window of squared filtered samples per band.
        self._theta_power_buf: deque[float] = deque(maxlen=SAMPLE_RATE * POWER_WINDOW_SEC)
        self._beta_power_buf:  deque[float] = deque(maxlen=SAMPLE_RATE * POWER_WINDOW_SEC)

        self.setWindowTitle(f"Muse 2 — Mind Monitor :{port}")
        self.resize(1280, 800)
        self.setStyleSheet("background-color: #000000; color: #ffffff;")

        pg.setConfigOptions(antialias=True, background="#000000", foreground="#ffffff")

        central = QtWidgets.QWidget()
        self._dashboard_page = central
        layout = QtWidgets.QHBoxLayout(central)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        # Left column: 5 stacked band plots.
        bands_widget = pg.GraphicsLayoutWidget()
        bands_widget.ci.setSpacing(2)
        bands_widget.ci.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(bands_widget, stretch=3)

        self.curves: dict[str, pg.PlotDataItem] = {}
        self.plots: dict[str, pg.PlotItem] = {}
        # Displayed y-range per band: snaps outward instantly (so live spikes
        # always fit), drifts inward slowly toward the rolling-window extremes.
        self.y_lo: dict[str, float] = {name: -1.0 for name in BANDS}
        self.y_hi: dict[str, float] = {name: 1.0 for name in BANDS}
        # Per-band history of (timestamp, chunk_min, chunk_max). Older entries
        # than RANGE_WINDOW_SEC are evicted each tick.
        self.range_history: dict[str, deque] = {name: deque() for name in BANDS}
        self.x_axis = np.arange(BUFFER_SIZE) / SAMPLE_RATE - WINDOW_SEC
        for i, name in enumerate(BANDS):
            plot = bands_widget.addPlot(row=i, col=0)
            plot.setTitle(name, size="14pt")
            plot.setMouseEnabled(x=False, y=False)
            plot.hideButtons()
            plot.showGrid(x=False, y=True, alpha=0.2)
            plot.setLabel("left", "µV")
            if i == len(BANDS) - 1:
                plot.setLabel("bottom", "seconds")
            else:
                plot.getAxis("bottom").setStyle(showValues=False)
            plot.setXRange(-WINDOW_SEC, 0, padding=0)
            plot.enableAutoRange(axis="y", enable=False)
            plot.setYRange(self.y_lo[name], self.y_hi[name], padding=0)
            r, g, b = BAND_COLORS[name]
            curve = plot.plot(self.x_axis, self.display[name],
                              pen=pg.mkPen(color=(r, g, b), width=1.4))
            self.curves[name] = curve
            self.plots[name] = plot

        # Right column: head plot + TBR/Pulse metrics + compact status text.
        right = QtWidgets.QWidget()
        right_layout = QtWidgets.QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(4)

        self._style_idx = 0
        self.current_style = HEAD_STYLES[self._style_idx]

        gyro_widget = pg.GraphicsLayoutWidget()
        gyro_widget.ci.setSpacing(0)
        gyro_widget.ci.setContentsMargins(0, 32, 0, 0)
        self.head_plot = gyro_widget.addPlot()
        self.head_plot.setTitle("<b>Head orientation</b>", size="20pt")
        self.head_plot.setMouseEnabled(x=False, y=False)
        self.head_plot.hideButtons()
        self.head_plot.setAspectLocked(True)
        self.head_plot.setXRange(-1.9, 1.9, padding=0)
        self.head_plot.setYRange(-1.9, 1.9, padding=0)
        self.head_plot.hideAxis("left")
        self.head_plot.hideAxis("bottom")

        self.orientation = OrientationTracker()
        # Depth-banded rendering: each band has its own pen (back = dim, front =
        # bright). The MediaPipe face mesh is one layer, the back-of-head
        # ellipsoid wireframe is a second layer with a cooler palette so the
        # face reads as a face.
        self.face_band_items: list[pg.PlotDataItem] = []
        self.back_band_items: list[pg.PlotDataItem] = []
        for b in range(DEPTH_BANDS):
            t = b / max(1, DEPTH_BANDS - 1)  # 0 = back, 1 = front
            back_pen = pg.mkPen(
                color=(60 + int(50 * t), 150 + int(80 * t),
                       200 + int(50 * t), 25 + int(180 * t)),
                width=0.8 + 0.5 * t,
            )
            face_pen = pg.mkPen(
                color=(220, 200 + int(40 * t), 130 + int(100 * t),
                       60 + int(190 * t)),
                width=0.8 + 0.7 * t,
            )
            self.back_band_items.append(
                self.head_plot.plot([], [], pen=back_pen, connect="pairs"))
            self.face_band_items.append(
                self.head_plot.plot([], [], pen=face_pen, connect="pairs"))
        self._render_head()
        right_layout.addWidget(gyro_widget, stretch=5)

        # Metrics row: text description on the left, TBR gauge on the right.
        metrics_widget = QtWidgets.QWidget()
        metrics_widget.setMinimumHeight(170)
        metrics_widget.setMaximumHeight(220)
        metrics_layout = QtWidgets.QHBoxLayout(metrics_widget)
        metrics_layout.setContentsMargins(8, 4, 4, 4)
        metrics_layout.setSpacing(8)

        text_panel = QtWidgets.QWidget()
        text_layout = QtWidgets.QVBoxLayout(text_panel)
        text_layout.setContentsMargins(8, 4, 8, 4)
        text_layout.setSpacing(8)

        title_label = QtWidgets.QLabel("Theta / Beta Ratio")
        title_label.setStyleSheet(
            "color: #ffffff; font: bold 20pt 'sans-serif';"
            "background-color: transparent;")
        title_label.setAlignment(QtCore.Qt.AlignCenter)

        desc_label = QtWidgets.QLabel(
            "A higher ratio reflects a more relaxed or drowsy state, "
            "a lower ratio reflects focused alertness.")
        desc_label.setWordWrap(True)
        desc_label.setStyleSheet(
            "color: #ffffff; font: 14pt 'sans-serif';"
            "background-color: transparent;")
        desc_label.setAlignment(QtCore.Qt.AlignCenter)

        self.tbr_value_label = QtWidgets.QLabel("TBR: —")
        self.tbr_value_label.setStyleSheet(
            "color: #ffffff; font: bold 18pt 'monospace';"
            "background-color: transparent;")
        self.tbr_value_label.setAlignment(QtCore.Qt.AlignCenter)

        text_layout.addStretch(1)
        text_layout.addWidget(title_label)
        text_layout.addWidget(desc_label)
        text_layout.addWidget(self.tbr_value_label)
        text_layout.addStretch(1)
        metrics_layout.addWidget(text_panel, stretch=3)

        # TBR gauge: numeric range 0..5, visual 50% point at TBR_GAUGE_REF (=2).
        # Yellow bar grows up from the reference when TBR > 2, blue bar grows
        # down when TBR < 2. Pixel-per-ratio differs above vs. below by design.
        tbr_graphics = pg.GraphicsLayoutWidget()
        tbr_graphics.ci.setSpacing(0)
        tbr_graphics.ci.setContentsMargins(0, 0, 0, 0)
        self.tbr_plot_item = tbr_graphics.addPlot()
        self.tbr_plot_item.setMouseEnabled(False, False)
        self.tbr_plot_item.hideButtons()
        self.tbr_plot_item.hideAxis("bottom")
        self.tbr_plot_item.setXRange(-0.6, 0.6, padding=0)
        self.tbr_plot_item.setYRange(TBR_GAUGE_MIN, TBR_GAUGE_MAX, padding=0.02)
        self.tbr_plot_item.getAxis("left").setTicks([[
            (tbr_to_display(v), f"{v:g}")
            for v in (0.0, 1.0, TBR_GAUGE_REF, 3.0, 4.0, 5.0)
        ]])
        self.tbr_plot_item.showGrid(False, True, alpha=0.2)
        self.tbr_bar = pg.BarGraphItem(
            x=[0.0], y0=[_TBR_DISPLAY_MID], height=[0.0], width=0.8,
            brush=pg.mkBrush(220, 200, 70), pen=pg.mkPen(None))
        self.tbr_plot_item.addItem(self.tbr_bar)
        self.tbr_plot_item.addItem(pg.InfiniteLine(
            pos=_TBR_DISPLAY_MID, angle=0,
            pen=pg.mkPen(color=(200, 200, 220), width=1.2, style=QtCore.Qt.DashLine)))
        tbr_graphics.setMinimumWidth(120)
        tbr_graphics.setMaximumWidth(180)
        metrics_layout.addWidget(tbr_graphics, stretch=1)
        right_layout.addWidget(metrics_widget, stretch=2)

        # Recording controls: editable label/subject + a REC indicator.
        rec_row = QtWidgets.QWidget()
        rec_layout = QtWidgets.QHBoxLayout(rec_row)
        rec_layout.setContentsMargins(8, 0, 8, 0)
        rec_layout.setSpacing(6)

        self.rec_indicator = QtWidgets.QLabel("● REC")
        self.rec_indicator.setStyleSheet(
            "color: #ff3b3b; font: bold 13pt 'monospace';"
            "background-color: transparent;")
        self.rec_indicator.setVisible(False)

        label_caption = QtWidgets.QLabel("label")
        label_caption.setStyleSheet("color: #aaaaaa; font: 10pt 'sans-serif';")
        self.label_edit = QtWidgets.QLineEdit(self._default_label)
        self.label_edit.setMaximumWidth(140)
        self.label_edit.setStyleSheet(
            "color: #ffffff; background-color: #222; border: 1px solid #444;"
            "padding: 2px;")

        subject_caption = QtWidgets.QLabel("subject")
        subject_caption.setStyleSheet("color: #aaaaaa; font: 10pt 'sans-serif';")
        self.subject_edit = QtWidgets.QLineEdit(self._default_subject)
        self.subject_edit.setMaximumWidth(140)
        self.subject_edit.setStyleSheet(
            "color: #ffffff; background-color: #222; border: 1px solid #444;"
            "padding: 2px;")

        rec_layout.addWidget(self.rec_indicator)
        rec_layout.addStretch(1)
        rec_layout.addWidget(label_caption)
        rec_layout.addWidget(self.label_edit)
        rec_layout.addWidget(subject_caption)
        rec_layout.addWidget(self.subject_edit)
        rec_row.setMaximumHeight(40)
        right_layout.addWidget(rec_row)

        self.status_label = QtWidgets.QLabel(f"Waiting for data on :{port}…")
        self.status_label.setStyleSheet(
            "color: #ffffff; font: 11pt 'monospace'; padding: 2px 6px;"
            "background-color: transparent;")
        self.status_label.setMaximumHeight(40)
        right_layout.addWidget(self.status_label)

        layout.addWidget(right, stretch=2)

        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(int(1000 / UPDATE_HZ))

        self._last_count = 0
        self._stat_timer = QtCore.QTimer(self)
        self._stat_timer.timeout.connect(self._update_status)
        self._stat_timer.start(1000)

        self._style_shortcut = QtWidgets.QShortcut(
            QtGui.QKeySequence("h"), self, activated=self._cycle_head_style)
        self._record_shortcut = QtWidgets.QShortcut(
            QtGui.QKeySequence("r"), self, activated=self._toggle_record)

        self.stack = QtWidgets.QStackedWidget()
        self.stack.addWidget(self._dashboard_page)      # page 0: dashboard
        self.nf_view = NeurofeedbackView(
            self.receiver, self.recorder,
            label_fn=lambda: self.label_edit.text().strip() or self._default_label,
            subject_fn=lambda: self.subject_edit.text().strip(),
            n_cues=self._nf_cues, seed=self._nf_seed)
        self.stack.addWidget(self.nf_view)              # page 1: neurofeedback
        self.setCentralWidget(self.stack)

        self._view_shortcut = QtWidgets.QShortcut(
            QtGui.QKeySequence("n"), self, activated=self._toggle_view)
        self._calib_shortcut = QtWidgets.QShortcut(
            QtGui.QKeySequence("c"), self, activated=self._nf_calibrate)
        self._recenter_shortcut = QtWidgets.QShortcut(
            QtGui.QKeySequence("0"), self, activated=self._nf_recenter)
        self._trial_shortcut = QtWidgets.QShortcut(
            QtGui.QKeySequence("t"), self, activated=self._nf_toggle_session)

    def _toggle_view(self):
        page = (self._dashboard_page if self.stack.currentWidget() is self.nf_view
                else self.nf_view)
        self.stack.setCurrentWidget(page)

    def _nf_calibrate(self):
        if self.stack.currentWidget() is self.nf_view:
            self.nf_view.start_calibration()

    def _nf_recenter(self):
        if self.stack.currentWidget() is self.nf_view:
            self.nf_view.recenter()

    def _nf_toggle_session(self):
        if self.stack.currentWidget() is self.nf_view:
            self.nf_view.toggle_session()

    def _cycle_head_style(self):
        self._style_idx = (self._style_idx + 1) % len(HEAD_STYLES)
        self.current_style = HEAD_STYLES[self._style_idx]
        self._render_head()

    def _toggle_record(self):
        if self.stack.currentWidget() is self.nf_view or self.nf_view.session is not None:
            return  # 'r' records only on the dashboard, and never while a cued session runs
        if self.recorder.active:
            session_dir = self.recorder.stop()
            self.rec_indicator.setVisible(False)
            self.label_edit.setEnabled(True)
            self.subject_edit.setEnabled(True)
            if session_dir is not None:
                self.status_label.setText(f"Saved recording → {session_dir}")
        else:
            label = self.label_edit.text().strip() or self._default_label
            subject = self.subject_edit.text().strip()
            try:
                self.recorder.start(label, subject)
            except OSError as exc:
                self.status_label.setText(f"Recording failed: {exc}")
                return
            self._rec_start_mono = time.monotonic()
            with self.receiver.lock:
                self._rec_start_count = self.receiver.eeg_count
            self.rec_indicator.setVisible(True)
            self.label_edit.setEnabled(False)
            self.subject_edit.setEnabled(False)

    def _render_head(self):
        R = self.orientation.matrix()
        s = self.current_style
        self._render_segments(s.cool_starts, s.cool_ends, self.back_band_items, R)
        self._render_segments(s.warm_starts, s.warm_ends, self.face_band_items, R)

    @staticmethod
    def _render_segments(starts, ends, items, R):
        starts_rot = starts @ R.T
        ends_rot = ends @ R.T
        mid_z = 0.5 * (starts_rot[:, 2] + ends_rot[:, 2])
        z_min, z_max = -1.8, 1.8
        band = np.clip(((mid_z - z_min) / (z_max - z_min) * DEPTH_BANDS).astype(int),
                       0, DEPTH_BANDS - 1)
        for b, item in enumerate(items):
            mask = band == b
            if not mask.any():
                item.setData([], [])
                continue
            s = starts_rot[mask]
            e = ends_rot[mask]
            n = mask.sum()
            xs = np.empty(n * 2)
            ys = np.empty(n * 2)
            xs[0::2] = s[:, 0]
            xs[1::2] = e[:, 0]
            ys[0::2] = s[:, 1]
            ys[1::2] = e[:, 1]
            item.setData(xs, ys, connect="pairs")

    def _tick(self):
        # Head updates each tick using the latest gyro snapshot, independent
        # of EEG arrival rate.
        self.orientation.set_from_gyro(self.receiver.latest_gyro())
        self._render_head()

        chunk = self.receiver.drain_eeg()
        if chunk.size == 0:
            return
        filtered = self.filters.process(chunk)
        n = chunk.size
        now = time.monotonic()
        cutoff = now - RANGE_WINDOW_SEC

        # Per-band squared samples feed the rolling-power buffers used by TBR.
        self._theta_power_buf.extend(np.square(filtered["Theta (4-8 Hz)"]))
        self._beta_power_buf.extend(np.square(filtered["Beta  (13-30 Hz)"]))

        for name, y in filtered.items():
            buf = self.display[name]
            if n >= BUFFER_SIZE:
                buf[:] = y[-BUFFER_SIZE:]
            else:
                buf[:-n] = buf[n:]
                buf[-n:] = y
            self.curves[name].setData(self.x_axis, buf)

            hist = self.range_history[name]
            hist.append((now, float(y.min()), float(y.max())))
            while hist and hist[0][0] < cutoff:
                hist.popleft()
            target_lo = min(h[1] for h in hist)
            target_hi = max(h[2] for h in hist)
            if target_hi - target_lo < 2 * MIN_HALF_RANGE:
                mid = 0.5 * (target_hi + target_lo)
                target_lo = mid - MIN_HALF_RANGE
                target_hi = mid + MIN_HALF_RANGE

            # Asymmetric tracking: expand instantly, contract slowly.
            self.y_hi[name] = (target_hi if target_hi > self.y_hi[name]
                               else self.y_hi[name] + RANGE_CONTRACT_ALPHA * (target_hi - self.y_hi[name]))
            self.y_lo[name] = (target_lo if target_lo < self.y_lo[name]
                               else self.y_lo[name] + RANGE_CONTRACT_ALPHA * (target_lo - self.y_lo[name]))
            self.plots[name].setYRange(self.y_lo[name], self.y_hi[name], padding=0.05)

    def _update_status(self):
        with self.receiver.lock:
            count = self.receiver.eeg_count
        rate = count - self._last_count
        self._last_count = count
        gx, gy, gz = self.receiver.latest_gyro()

        # Theta/Beta ratio. Prefer Mind Monitor's pre-computed absolute band
        # powers (log10, per-channel; averaged across the 4 sensors) — this
        # matches the Muse SDK / Lubar-Monastra research convention. Fall back
        # to our own raw-EEG band-pass power ratio if MM isn't sending bands.
        theta_log, beta_log = self.receiver.latest_band_powers()
        tbr = float("nan")
        if not math.isnan(theta_log) and not math.isnan(beta_log):
            tbr = 10.0 ** (theta_log - beta_log)
        elif self._theta_power_buf and self._beta_power_buf:
            tp = float(np.mean(self._theta_power_buf))
            bp = float(np.mean(self._beta_power_buf))
            tbr = tp / max(bp, 1e-9)

        if not math.isnan(tbr):
            display_y = tbr_to_display(tbr)
            height = display_y - _TBR_DISPLAY_MID
            color = (220, 200, 70) if height >= 0 else (90, 130, 220)
            self.tbr_bar.setOpts(y0=[_TBR_DISPLAY_MID], height=[height],
                                 brush=pg.mkBrush(*color))
            self.tbr_value_label.setText(f"TBR: {tbr:5.2f}")
        else:
            self.tbr_bar.setOpts(height=[0.0])
            self.tbr_value_label.setText("TBR: —")

        rec_suffix = ""
        if self.recorder.active:
            elapsed = time.monotonic() - self._rec_start_mono
            rec_samples = count - self._rec_start_count
            mm, ss = divmod(int(elapsed), 60)
            rec_suffix = f"   REC {mm:02d}:{ss:02d} · {rec_samples} samples"
            self.rec_indicator.setText("● REC" if int(elapsed) % 2 == 0 else "○ REC")
        self.status_label.setText(
            f"EEG samples {count:>7d}  rate {rate:>3d} Hz   "
            f"gyro {gx:+6.1f} {gy:+6.1f} {gz:+6.1f}{rec_suffix}"
        )


def main():
    parser = argparse.ArgumentParser(description="Muse 2 OSC visualizer")
    parser.add_argument("--host", default="0.0.0.0", help="bind address (default: all interfaces)")
    parser.add_argument("--port", type=int, default=5000, help="OSC UDP port (default: 5000)")
    parser.add_argument("--label", default="muse",
                        help="default session label / device name (editable in-app)")
    parser.add_argument("--subject", default="",
                        help="default subject name (editable in-app)")
    parser.add_argument("--rec-dir", default="recordings",
                        help="directory to write session recordings into")
    parser.add_argument("--nf-trials", type=int, default=8,
                        help="number of cued FOCUS/RELAX trials per session")
    parser.add_argument("--nf-seed", type=int, default=0,
                        help="random seed for cued-trial order")
    args = parser.parse_args()

    receiver = OSCReceiver(host=args.host, port=args.port)
    receiver.start()
    print(f"OSC server listening on {args.host}:{args.port}", file=sys.stderr)

    app = QtWidgets.QApplication(sys.argv)
    win = MuseDashboard(receiver, args.port, rec_dir=args.rec_dir,
                        label=args.label, subject=args.subject,
                        nf_cues=args.nf_trials, nf_seed=args.nf_seed)
    win.show()
    try:
        rc = app.exec_()
    finally:
        win.nf_view.shutdown()   # flush cues/feedback if a cued session is open
        win.recorder.stop()      # finalize meta.json + close files if mid-recording
        receiver.stop()
    sys.exit(rc)


if __name__ == "__main__":
    main()
