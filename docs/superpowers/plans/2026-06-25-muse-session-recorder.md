# Muse 2 Session Recorder Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a labeled, per-stream CSV session recorder to the Muse 2 visualizer so the user can capture one headset at a time (press `r`) and accumulate a dataset.

**Architecture:** A standalone, fully-unit-testable `SessionRecorder` class writes one CSV per OSC stream into a timestamped/labeled session folder plus a `meta.json` sidecar. The existing `OSCReceiver` taps every incoming OSC message (raw, un-averaged) to the recorder when active. The Qt dashboard gets an `r` hotkey, label/subject fields, and a REC indicator.

**Tech Stack:** Python 3, `python-osc`, PyQt5/pyqtgraph (existing), `csv`/`json` stdlib, `pytest` for tests.

---

## File Structure

| File | Responsibility |
|------|----------------|
| `session_recorder.py` (new) | `SessionRecorder`: session lifecycle, stream routing, CSV + meta.json writing, thread-safety |
| `test_session_recorder.py` (new) | Unit + integration tests (headless) |
| `muse_visualizer.py` (modify) | `OSCReceiver` tap; dashboard hotkey/fields/indicator; CLI flags |
| `fake_muse.py` (modify) | Emit `/muse/acc` + `/muse/elements/*` for smoke-testing |
| `.gitignore` (modify) | Ignore `recordings/` |
| `requirements.txt` (modify) | Add `pytest` |

All files live at repo root (the project is flat). Tests import `from session_recorder import SessionRecorder` and run from repo root.

---

### Task 1: Project setup

**Files:**
- Modify: `.gitignore`
- Modify: `requirements.txt`

- [ ] **Step 1: Add `recordings/` to `.gitignore`**

Append this line to `.gitignore`:

```
recordings/
```

- [ ] **Step 2: Add pytest to `requirements.txt`**

Append this line to `requirements.txt`:

```
pytest>=8.0
```

- [ ] **Step 3: Install pytest into the existing venv**

Run: `.venv/bin/python -m pip install "pytest>=8.0"`
Expected: ends with `Successfully installed pytest-...` (or "already satisfied").

- [ ] **Step 4: Verify pytest runs**

Run: `.venv/bin/python -m pytest --version`
Expected: prints `pytest 8.x.y`.

- [ ] **Step 5: Commit**

```bash
git add .gitignore requirements.txt
git commit -m "chore: ignore recordings/, add pytest dev dependency"
```

---

### Task 2: SessionRecorder — start/stop lifecycle + meta.json

**Files:**
- Create: `session_recorder.py`
- Test: `test_session_recorder.py`

- [ ] **Step 1: Write the failing test**

Create `test_session_recorder.py`:

```python
import json
from datetime import datetime
from pathlib import Path

import pytest

from session_recorder import SessionRecorder


class FakeClock:
    """Monotonic stand-in whose value the test sets explicitly."""
    def __init__(self, t=0.0):
        self.t = t
    def __call__(self):
        return self.t


FIXED_DT = datetime(2026, 6, 25, 14, 30, 0)


def make_recorder(tmp_path, clock=None):
    return SessionRecorder(
        base_dir=tmp_path,
        now_fn=lambda: FIXED_DT,
        clock_fn=clock or FakeClock(),
    )


def test_start_creates_labeled_folder_and_recording_meta(tmp_path):
    rec = make_recorder(tmp_path)
    session_dir = rec.start("museA", subject="dan")

    assert session_dir == tmp_path / "20260625-143000_museA"
    assert session_dir.is_dir()
    assert rec.active is True

    meta = json.loads((session_dir / "meta.json").read_text())
    assert meta["status"] == "recording"
    assert meta["label"] == "museA"
    assert meta["subject"] == "dan"
    assert meta["start_iso"] == "2026-06-25T14:30:00"
    assert meta["end_iso"] is None
    assert meta["duration_s"] is None


def test_stop_finalizes_meta_with_duration_and_complete_status(tmp_path):
    clock = FakeClock(10.0)
    rec = make_recorder(tmp_path, clock=clock)
    session_dir = rec.start("museA")          # t0 = 10.0
    clock.t = 15.5                             # 5.5 s later
    returned = rec.stop()

    assert returned == session_dir
    assert rec.active is False
    meta = json.loads((session_dir / "meta.json").read_text())
    assert meta["status"] == "complete"
    assert meta["end_iso"] == "2026-06-25T14:30:00"
    assert meta["duration_s"] == 5.5
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest test_session_recorder.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'session_recorder'`.

- [ ] **Step 3: Write minimal implementation**

Create `session_recorder.py`:

```python
"""Records a Muse 2 OSC session to per-stream CSV files + a meta.json sidecar.

Designed to be driven from the OSC server thread (record) and the GUI thread
(start/stop) concurrently, so all state transitions are guarded by a lock.
Has no GUI or network dependency, so it is fully unit-testable.
"""

from __future__ import annotations

import csv
import json
import threading
import time
from datetime import datetime
from pathlib import Path


class SessionRecorder:
    # OSC address -> (filename, fixed data columns excluding the leading `t`).
    FIXED = {
        "/muse/eeg":  ("eeg.csv",  ["TP9", "AF7", "AF8", "TP10"]),
        "/muse/gyro": ("gyro.csv", ["x", "y", "z"]),
        "/muse/acc":  ("acc.csv",  ["x", "y", "z"]),
        "/muse/ppg":  ("ppg.csv",  ["ppg1", "ppg2", "ppg3"]),
    }

    def __init__(self, base_dir="recordings", now_fn=None, clock_fn=None):
        self.base_dir = Path(base_dir)
        self._now = now_fn or datetime.now
        self._clock = clock_fn or time.monotonic
        self._lock = threading.Lock()
        self.active = False
        self._session_dir = None
        self._files = {}       # filename -> open file handle
        self._writers = {}     # filename -> csv.writer
        self._counts = {}      # filename -> row count
        self._columns = {}     # filename -> data columns (excluding `t`)
        self._addresses = set()
        self._t0 = 0.0
        self._start_dt = None
        self._label = ""
        self._subject = ""
        self._errors = 0

    @staticmethod
    def _safe(label):
        cleaned = "".join(c if c.isalnum() or c in "-_" else "_" for c in label)
        return cleaned or "muse"

    def start(self, label, subject=""):
        with self._lock:
            if self.active:
                return self._session_dir
            self._start_dt = self._now()
            self._t0 = self._clock()
            self._label = label
            self._subject = subject
            name = self._start_dt.strftime("%Y%m%d-%H%M%S") + "_" + self._safe(label)
            self._session_dir = self.base_dir / name
            self._session_dir.mkdir(parents=True, exist_ok=True)
            self._files = {}
            self._writers = {}
            self._counts = {}
            self._columns = {}
            self._addresses = set()
            self._errors = 0
            self.active = True
            self._write_meta("recording")
            return self._session_dir

    def stop(self):
        with self._lock:
            if not self.active:
                return None
            self.active = False
            for f in self._files.values():
                try:
                    f.flush()
                    f.close()
                except Exception:
                    pass
            self._write_meta("complete")
            session_dir = self._session_dir
            self._files = {}
            self._writers = {}
            return session_dir

    def _write_meta(self, status):
        meta = {
            "label": self._label,
            "subject": self._subject,
            "status": status,
            "start_iso": self._start_dt.isoformat(timespec="seconds"),
            "end_iso": None,
            "duration_s": None,
            "streams": dict(self._counts),
            "columns": {k: ["t"] + v for k, v in self._columns.items()},
            "addresses": sorted(self._addresses),
            "errors": self._errors,
        }
        if status == "complete":
            meta["end_iso"] = self._now().isoformat(timespec="seconds")
            meta["duration_s"] = round(self._clock() - self._t0, 3)
        with open(self._session_dir / "meta.json", "w") as f:
            json.dump(meta, f, indent=2)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest test_session_recorder.py -v`
Expected: both tests PASS.

- [ ] **Step 5: Commit**

```bash
git add session_recorder.py test_session_recorder.py
git commit -m "feat: SessionRecorder start/stop lifecycle with meta.json"
```

---

### Task 3: SessionRecorder — record() routing for fixed streams

**Files:**
- Modify: `session_recorder.py`
- Test: `test_session_recorder.py`

- [ ] **Step 1: Write the failing test**

Append to `test_session_recorder.py`:

```python
def _read_csv(path):
    lines = Path(path).read_text().strip().splitlines()
    return [ln.split(",") for ln in lines]


def test_records_fixed_streams_to_their_own_csvs(tmp_path):
    rec = make_recorder(tmp_path)
    sd = rec.start("museA")
    rec.record("/muse/eeg", [1.0, 2.0, 3.0, 4.0], t=0.0)
    rec.record("/muse/eeg", [5.0, 6.0, 7.0, 8.0], t=0.5)
    rec.record("/muse/gyro", [10.0, 11.0, 12.0], t=0.5)
    rec.stop()

    eeg = _read_csv(sd / "eeg.csv")
    assert eeg[0] == ["t", "TP9", "AF7", "AF8", "TP10"]
    assert eeg[1] == ["0.000000", "1.0", "2.0", "3.0", "4.0"]
    assert eeg[2] == ["0.500000", "5.0", "6.0", "7.0", "8.0"]

    gyro = _read_csv(sd / "gyro.csv")
    assert gyro[0] == ["t", "x", "y", "z"]
    assert gyro[1] == ["0.500000", "10.0", "11.0", "12.0"]

    meta = json.loads((sd / "meta.json").read_text())
    assert meta["streams"]["eeg.csv"] == 2
    assert meta["streams"]["gyro.csv"] == 1
    assert meta["addresses"] == ["/muse/eeg", "/muse/gyro"]
    assert meta["columns"]["eeg.csv"] == ["t", "TP9", "AF7", "AF8", "TP10"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest test_session_recorder.py::test_records_fixed_streams_to_their_own_csvs -v`
Expected: FAIL — `AttributeError: 'SessionRecorder' object has no attribute 'record'`.

- [ ] **Step 3: Write minimal implementation**

Add these methods to `SessionRecorder` (in `session_recorder.py`, after `start`):

```python
    def record(self, addr, args, t=None):
        with self._lock:
            if not self.active:
                return
            try:
                if t is None:
                    t = self._clock() - self._t0
                self._addresses.add(addr)
                fname, cols, row = self._route(addr, list(args))
                writer = self._writer_for(fname, cols)
                writer.writerow([f"{t:.6f}"] + row)
                self._counts[fname] = self._counts.get(fname, 0) + 1
            except Exception:
                self._errors += 1

    def _route(self, addr, args):
        if addr in self.FIXED:
            fname, base = self.FIXED[addr]
            cols = self._columns.get(fname)
            if cols is None:  # lock column count from the first message
                ncols = max(len(base), len(args))
                cols = base + [f"v{i}" for i in range(len(base), ncols)]
            ncols = len(cols)
            row = [str(a) for a in args[:ncols]]
            row += [""] * (ncols - len(row))
            return fname, cols, row
        # (elements + catch-all added in Task 4)
        return "other.csv", ["addr", "values"], [addr, "|".join(str(a) for a in args)]

    def _writer_for(self, fname, cols):
        writer = self._writers.get(fname)
        if writer is None:
            f = open(self._session_dir / fname, "w", newline="", buffering=1)
            self._files[fname] = f
            writer = csv.writer(f)
            writer.writerow(["t"] + cols)
            self._writers[fname] = writer
            self._counts.setdefault(fname, 0)
            self._columns[fname] = cols
        return writer
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest test_session_recorder.py -v`
Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add session_recorder.py test_session_recorder.py
git commit -m "feat: record fixed OSC streams (eeg/gyro/acc/ppg) to per-stream CSVs"
```

---

### Task 4: SessionRecorder — elements, catch-all, variable arity, robustness

**Files:**
- Modify: `session_recorder.py`
- Test: `test_session_recorder.py`

- [ ] **Step 1: Write the failing tests**

Append to `test_session_recorder.py`:

```python
def test_elements_go_to_long_format_csv(tmp_path):
    rec = make_recorder(tmp_path)
    sd = rec.start("museA")
    rec.record("/muse/elements/alpha_absolute", [0.1, 0.2, 0.3, 0.4], t=0.0)
    rec.record("/muse/elements/blink", [1], t=0.1)
    rec.stop()

    rows = _read_csv(sd / "elements.csv")
    assert rows[0] == ["t", "addr", "v0", "v1", "v2", "v3"]
    assert rows[1] == ["0.000000", "/muse/elements/alpha_absolute", "0.1", "0.2", "0.3", "0.4"]
    assert rows[2] == ["0.100000", "/muse/elements/blink", "1", "", "", ""]


def test_unmapped_address_goes_to_other_csv(tmp_path):
    rec = make_recorder(tmp_path)
    sd = rec.start("museA")
    rec.record("/muse/batt", [88, 4100, 3600, 0], t=0.0)
    rec.stop()

    rows = _read_csv(sd / "other.csv")
    assert rows[0] == ["t", "addr", "values"]
    assert rows[1] == ["0.000000", "/muse/batt", "88|4100|3600|0"]


def test_variable_arity_locks_columns_from_first_message(tmp_path):
    rec = make_recorder(tmp_path)
    sd = rec.start("museA")
    rec.record("/muse/eeg", [1.0, 2.0, 3.0, 4.0, 9.0], t=0.0)  # 5-channel (AUX)
    rec.record("/muse/eeg", [1.0, 2.0, 3.0, 4.0], t=0.1)       # 4-channel later
    rec.stop()

    rows = _read_csv(sd / "eeg.csv")
    assert rows[0] == ["t", "TP9", "AF7", "AF8", "TP10", "v4"]
    assert rows[1] == ["0.000000", "1.0", "2.0", "3.0", "4.0", "9.0"]
    assert rows[2] == ["0.100000", "1.0", "2.0", "3.0", "4.0", ""]


def test_record_while_inactive_is_noop(tmp_path):
    rec = make_recorder(tmp_path)
    rec.record("/muse/eeg", [1.0, 2.0, 3.0, 4.0], t=0.0)  # before start
    assert rec.active is False
    sd = rec.start("museA")
    rec.stop()
    rec.record("/muse/eeg", [1.0, 2.0, 3.0, 4.0], t=0.0)  # after stop
    assert not (sd / "eeg.csv").exists()


def test_stop_is_idempotent(tmp_path):
    rec = make_recorder(tmp_path)
    rec.start("museA")
    assert rec.stop() is not None
    assert rec.stop() is None


def test_malformed_message_is_counted_not_raised(tmp_path):
    rec = make_recorder(tmp_path)
    sd = rec.start("museA")
    rec.record("/muse/eeg", [1.0, 2.0, 3.0, 4.0], t=None)  # t=None ok (uses clock)
    # A value that blows up str()? Use an object whose __str__ raises.
    class Boom:
        def __str__(self):
            raise ValueError("boom")
    rec.record("/muse/eeg", [Boom()], t=0.0)
    rec.stop()
    meta = json.loads((sd / "meta.json").read_text())
    assert meta["errors"] == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest test_session_recorder.py -v`
Expected: `test_elements_go_to_long_format_csv` FAILS (elements currently route to `other.csv`). The arity/noop/idempotent/error tests should already pass given Task 3 code; confirm which fail before editing.

- [ ] **Step 3: Write minimal implementation**

In `session_recorder.py`, replace the `_route` method body's catch-all section so elements are handled before the fallback. The full method becomes:

```python
    def _route(self, addr, args):
        if addr in self.FIXED:
            fname, base = self.FIXED[addr]
            cols = self._columns.get(fname)
            if cols is None:  # lock column count from the first message
                ncols = max(len(base), len(args))
                cols = base + [f"v{i}" for i in range(len(base), ncols)]
            ncols = len(cols)
            row = [str(a) for a in args[:ncols]]
            row += [""] * (ncols - len(row))
            return fname, cols, row
        if addr.startswith("/muse/elements/"):
            vals = [str(a) for a in args[:4]]
            vals += [""] * (4 - len(vals))
            return "elements.csv", ["addr", "v0", "v1", "v2", "v3"], [addr] + vals
        return "other.csv", ["addr", "values"], [addr, "|".join(str(a) for a in args)]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest test_session_recorder.py -v`
Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add session_recorder.py test_session_recorder.py
git commit -m "feat: route elements/catch-all streams, handle variable arity + errors"
```

---

### Task 5: Wire the recorder into OSCReceiver

**Files:**
- Modify: `muse_visualizer.py` (`OSCReceiver`: import, `__init__`, handlers, `_on_default`)
- Test: `test_session_recorder.py`

- [ ] **Step 1: Write the failing integration test**

Append to `test_session_recorder.py`:

```python
def test_receiver_taps_raw_eeg_and_unmapped_streams(tmp_path):
    from muse_visualizer import OSCReceiver

    rec = make_recorder(tmp_path)
    receiver = OSCReceiver(host="127.0.0.1", port=0)  # ephemeral port, no thread
    receiver.recorder = rec
    try:
        sd = rec.start("museA")
        # Raw 4-channel EEG must be stored un-averaged.
        receiver._on_eeg("/muse/eeg", 1.0, 2.0, 3.0, 4.0)
        # Unmapped stream reaches the recorder via the default handler.
        receiver._on_default("/muse/acc", 0.1, 0.2, 9.8)
        rec.stop()
    finally:
        receiver.server.server_close()

    eeg = _read_csv(sd / "eeg.csv")
    assert eeg[0] == ["t", "TP9", "AF7", "AF8", "TP10"]
    assert eeg[1][1:] == ["1.0", "2.0", "3.0", "4.0"]  # raw, not the mean (2.5)

    acc = _read_csv(sd / "acc.csv")
    assert acc[0] == ["t", "x", "y", "z"]
    assert acc[1][1:] == ["0.1", "0.2", "9.8"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest test_session_recorder.py::test_receiver_taps_raw_eeg_and_unmapped_streams -v`
Expected: FAIL — `acc.csv` is not created (current `_on_default` is a no-op) and/or `receiver.recorder` attribute does not exist.

- [ ] **Step 3: Write minimal implementation**

In `muse_visualizer.py`:

(a) Add the import near the top (after the existing imports, before `SAMPLE_RATE`):

```python
from session_recorder import SessionRecorder
```

(b) In `OSCReceiver.__init__`, add a recorder slot. Find this line:

```python
        self.gyro = np.zeros(3, dtype=np.float64)  # x, y, z
```

and insert immediately after it:

```python
        self.recorder = None  # set to a SessionRecorder to capture raw OSC
```

(c) Add the tap helper. Insert this method into `OSCReceiver` right before `_on_eeg`:

```python
    def _tap(self, addr, args):
        rec = self.recorder
        if rec is not None and rec.active:
            rec.record(addr, args)
```

(d) Forward raw args from each handler. Change the four handler signatures from `_addr` to `addr` and add a `_tap` call as the first line of each body. The handlers become:

```python
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
```

(e) Replace the no-op default handler:

```python
    def _on_default(self, _addr, *_args):
        pass
```

with one that taps:

```python
    def _on_default(self, addr, *args):
        self._tap(addr, args)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest test_session_recorder.py -v`
Expected: all tests PASS (including the new integration test).

- [ ] **Step 5: Commit**

```bash
git add muse_visualizer.py test_session_recorder.py
git commit -m "feat: tap raw OSC messages from OSCReceiver into the recorder"
```

---

### Task 6: Dashboard hotkey, label/subject fields, REC indicator, CLI flags

**Files:**
- Modify: `muse_visualizer.py` (`MuseDashboard.__init__`, new methods, `main`)

This task is GUI wiring; it is verified by the end-to-end smoke test in Task 7. Make the edits exactly as shown.

- [ ] **Step 1: Accept recorder config in `MuseDashboard.__init__`**

Change the constructor signature. Find:

```python
    def __init__(self, receiver: OSCReceiver, port: int):
        super().__init__()
        self.receiver = receiver
        self.filters = FilterBank()
```

Replace with:

```python
    def __init__(self, receiver: OSCReceiver, port: int,
                 rec_dir: str = "recordings", label: str = "muse", subject: str = ""):
        super().__init__()
        self.receiver = receiver
        self.filters = FilterBank()
        self.recorder = SessionRecorder(base_dir=rec_dir)
        self.receiver.recorder = self.recorder
        self._default_label = label
        self._default_subject = subject
        self._rec_start_mono = 0.0
        self._rec_start_count = 0
```

- [ ] **Step 2: Add the label/subject fields + REC indicator to the right column**

Find the status label block near the end of `__init__`:

```python
        self.status_label = QtWidgets.QLabel(f"Waiting for data on :{port}…")
        self.status_label.setStyleSheet(
            "color: #ffffff; font: 11pt 'monospace'; padding: 2px 6px;"
            "background-color: transparent;")
        self.status_label.setMaximumHeight(40)
        right_layout.addWidget(self.status_label)
```

Insert the following BEFORE that block (so the recording row sits above the status line):

```python
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
```

- [ ] **Step 3: Register the `r` hotkey**

Find the existing style shortcut at the end of `__init__`:

```python
        self._style_shortcut = QtWidgets.QShortcut(
            QtGui.QKeySequence("h"), self, activated=self._cycle_head_style)
```

Insert immediately after it:

```python
        self._record_shortcut = QtWidgets.QShortcut(
            QtGui.QKeySequence("r"), self, activated=self._toggle_record)
```

- [ ] **Step 4: Add the toggle + status-helper methods**

Insert these methods into `MuseDashboard` right after `_cycle_head_style`:

```python
    def _toggle_record(self):
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
```

- [ ] **Step 5: Show elapsed time + sample count while recording**

In `_update_status`, find the final `self.status_label.setText(...)` call:

```python
        self.status_label.setText(
            f"EEG samples {count:>7d}  rate {rate:>3d} Hz   "
            f"gyro {gx:+6.1f} {gy:+6.1f} {gz:+6.1f}"
        )
```

Replace it with:

```python
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
```

- [ ] **Step 6: Add CLI flags and pass them through in `main`**

In `main`, find:

```python
    parser.add_argument("--port", type=int, default=5000, help="OSC UDP port (default: 5000)")
    args = parser.parse_args()
```

Insert the new flags before `args = parser.parse_args()`:

```python
    parser.add_argument("--label", default="muse",
                        help="default session label / device name (editable in-app)")
    parser.add_argument("--subject", default="",
                        help="default subject name (editable in-app)")
    parser.add_argument("--rec-dir", default="recordings",
                        help="directory to write session recordings into")
```

Then find:

```python
    win = MuseDashboard(receiver, args.port)
```

and replace with:

```python
    win = MuseDashboard(receiver, args.port, rec_dir=args.rec_dir,
                        label=args.label, subject=args.subject)
```

- [ ] **Step 7: Verify the module still imports and unit tests still pass**

Run: `.venv/bin/python -c "import muse_visualizer"`
Expected: no output, exit 0 (no syntax/import errors).

Run: `.venv/bin/python -m pytest test_session_recorder.py -v`
Expected: all tests PASS.

- [ ] **Step 8: Commit**

```bash
git add muse_visualizer.py
git commit -m "feat: r-hotkey recording with label/subject fields and REC indicator"
```

---

### Task 7: Extend fake_muse + end-to-end smoke test

**Files:**
- Modify: `fake_muse.py`

- [ ] **Step 1: Emit acc + elements from the fake client**

In `fake_muse.py`, find the gyro block inside the main loop:

```python
        if i % 32 == 0:  # 8 Hz gyro updates
            gx = 50 * math.sin(2 * math.pi * 0.2 * t)
            gy = 50 * math.cos(2 * math.pi * 0.2 * t)
            client.send_message("/muse/gyro", [gx, gy, 0.0])
```

Insert immediately after it (still inside the `while` loop):

```python
        if i % 32 == 0:  # 8 Hz accelerometer updates
            ax = 0.02 * math.sin(2 * math.pi * 0.2 * t)
            ay = 0.02 * math.cos(2 * math.pi * 0.2 * t)
            client.send_message("/muse/acc", [ax, ay, 1.0])

        if i % 256 == 0:  # 1 Hz band-power elements
            theta = -0.5 + 0.2 * math.sin(2 * math.pi * 0.1 * t)
            beta = -0.8 + 0.2 * math.cos(2 * math.pi * 0.1 * t)
            client.send_message("/muse/elements/theta_absolute", [theta] * 4)
            client.send_message("/muse/elements/beta_absolute", [beta] * 4)
```

- [ ] **Step 2: Headless end-to-end smoke test (scripted, no GUI)**

This drives the real `OSCReceiver` over a UDP socket from the fake client and confirms a full session folder is produced. Run this one-off command from the repo root:

```bash
.venv/bin/python - <<'PY'
import time, json, threading
from pathlib import Path
import tempfile
from pythonosc.udp_client import SimpleUDPClient
from muse_visualizer import OSCReceiver
from session_recorder import SessionRecorder

tmp = Path(tempfile.mkdtemp())
rec = SessionRecorder(base_dir=tmp)
receiver = OSCReceiver(host="127.0.0.1", port=5055)
receiver.recorder = rec
receiver.start()
rec.start("smoke", subject="tester")

client = SimpleUDPClient("127.0.0.1", 5055)
for k in range(512):                       # ~0.1 s of 256 Hz EEG
    client.send_message("/muse/eeg", [1.0, 2.0, 3.0, 4.0])
    if k % 32 == 0:
        client.send_message("/muse/acc", [0.1, 0.2, 1.0])
        client.send_message("/muse/gyro", [5.0, 6.0, 7.0])
    if k % 256 == 0:
        client.send_message("/muse/elements/alpha_absolute", [0.1, 0.2, 0.3, 0.4])
    time.sleep(0.0005)

time.sleep(0.3)                            # let the server thread drain
sd = rec.stop()
receiver.stop()

meta = json.loads((sd / "meta.json").read_text())
print("session dir:", sd)
print("files:", sorted(p.name for p in sd.iterdir()))
print("status:", meta["status"], "errors:", meta["errors"])
print("streams:", meta["streams"])
assert meta["status"] == "complete"
assert meta["streams"].get("eeg.csv", 0) > 100
assert (sd / "acc.csv").exists()
assert (sd / "elements.csv").exists()
print("SMOKE OK")
PY
```

Expected: prints `SMOKE OK`, with `eeg.csv` row count > 100 and `acc.csv`/`elements.csv` present.

- [ ] **Step 3: (Optional, manual) GUI smoke test**

In one terminal: `.venv/bin/python muse_visualizer.py --port 5055 --label museA --subject dan`
In another: `.venv/bin/python fake_muse.py --port 5055`
In the GUI: press `r` to start (red ● REC + elapsed appears), wait a few seconds, press `r` to stop. Confirm the status line shows `Saved recording → recordings/<timestamp>_museA` and that the folder contains populated CSVs and a `complete` `meta.json`.

- [ ] **Step 4: Commit**

```bash
git add fake_muse.py
git commit -m "feat: fake_muse emits acc + elements for end-to-end recorder smoke test"
```

---

## Notes for the implementer

- Run the full unit suite after each task: `.venv/bin/python -m pytest test_session_recorder.py -v`.
- The integration test constructs `OSCReceiver(port=0)`; it binds an ephemeral UDP socket but never starts the serve thread, and closes the socket in a `finally`. If you see `OSError: address already in use`, ensure no other test left a server open.
- CSV float formatting relies on Python's default `str(float)`; do not "clean up" values into fixed precision — faithfulness to the raw stream is the point.
- `t` is written with 6 decimal places (`f"{t:.6f}"`); everything else is written verbatim.
