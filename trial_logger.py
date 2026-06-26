"""Writes the two derived neurofeedback streams — feedback.csv (per frame) and
cues.csv (per trial) — into an existing recording session folder. The raw OSC
streams in the same folder are owned by SessionRecorder."""

from __future__ import annotations

import csv
from pathlib import Path


class TrialLogger:
    FEEDBACK_HEADER = ["t", "raw_signal", "smoothed", "drive_d", "cursor_x", "active_cue"]
    CUES_HEADER = ["t_start", "t_end", "cue", "goal_side", "hit", "dwell_s"]

    def __init__(self, session_dir):
        self._dir = Path(session_dir)
        self._fb_file = open(self._dir / "feedback.csv", "w", newline="", buffering=1)
        self._fb = csv.writer(self._fb_file)
        self._fb.writerow(self.FEEDBACK_HEADER)
        self._cue_file = None
        self._cue = None

    def log_feedback(self, t, raw, smoothed, drive, cursor_x, active_cue):
        self._fb.writerow([f"{t:.6f}", f"{raw:.6f}", f"{smoothed:.6f}",
                           f"{drive:.4f}", f"{cursor_x:.4f}", active_cue or ""])

    def log_trial(self, t_start, t_end, cue, goal_side, hit, dwell_s):
        if self._cue is None:
            self._cue_file = open(self._dir / "cues.csv", "w", newline="", buffering=1)
            self._cue = csv.writer(self._cue_file)
            self._cue.writerow(self.CUES_HEADER)
        self._cue.writerow([f"{t_start:.6f}", f"{t_end:.6f}", cue, goal_side,
                            int(bool(hit)), f"{dwell_s:.3f}"])

    def close(self):
        for f in (self._fb_file, self._cue_file):
            if f is not None:
                try:
                    f.flush()
                    f.close()
                except Exception:
                    pass
