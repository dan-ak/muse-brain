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
