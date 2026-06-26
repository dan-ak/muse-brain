import csv

from trial_logger import TrialLogger


def _rows(path):
    with open(path, newline="") as f:
        return list(csv.reader(f))


def test_feedback_csv_header_and_row(tmp_path):
    log = TrialLogger(tmp_path)
    log.log_feedback(t=0.5, raw=1.25, smoothed=1.20, drive=0.5,
                     cursor_x=0.3, active_cue="focus")
    log.close()
    rows = _rows(tmp_path / "feedback.csv")
    assert rows[0] == ["t", "raw_signal", "smoothed", "drive_d", "cursor_x", "active_cue"]
    assert rows[1] == ["0.500000", "1.250000", "1.200000", "0.5000", "0.3000", "focus"]


def test_feedback_blank_cue_when_none(tmp_path):
    log = TrialLogger(tmp_path)
    log.log_feedback(0.0, 0.0, 0.0, 0.0, 0.0, "")
    log.close()
    assert _rows(tmp_path / "feedback.csv")[1][5] == ""


def test_cues_csv_written_lazily_with_header(tmp_path):
    log = TrialLogger(tmp_path)
    assert not (tmp_path / "cues.csv").exists()   # not created until first trial
    log.log_trial(t_start=5.0, t_end=15.0, cue="focus",
                  goal_side="right", hit=True, dwell_s=2.0)
    log.close()
    rows = _rows(tmp_path / "cues.csv")
    assert rows[0] == ["t_start", "t_end", "cue", "goal_side", "hit", "dwell_s"]
    assert rows[1] == ["5.000000", "15.000000", "focus", "right", "1", "2.000"]


def test_close_is_safe_without_trials(tmp_path):
    log = TrialLogger(tmp_path)
    log.close()   # must not raise even though cues.csv was never opened
