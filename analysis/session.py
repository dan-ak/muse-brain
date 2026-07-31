"""Load a recorded session.

Sessions are plain CSV (optionally gzipped), so nothing here is required — you
can open them in a spreadsheet or read them with pandas directly. This exists to
save you working out the column layout.

    from analysis.session import Session
    s = Session("data/latest")
    eeg = s.eeg()                 # DataFrame: t, seat, TP9, AF7, AF8, TP10
    for trial in s.trials():      # only if the session used the cued protocol
        print(trial.eyes, trial.task, trial.eeg().shape)
"""

from __future__ import annotations

import gzip
import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

FS = 256  # EEG sample rate, Hz
EEG_CHANNELS = ["TP9", "AF7", "AF8", "TP10"]


def _open(path: Path):
    """Read a stream whether or not it was gzipped when published."""
    if path.exists():
        return path
    gz = path.with_suffix(path.suffix + ".gz")
    if gz.exists():
        return gz
    raise FileNotFoundError(f"no {path.name} (or .gz) in {path.parent}")


@dataclass
class Trial:
    index: int
    eyes: str          # "open" | "closed"
    task: str          # "calm" | "focus"
    start: float       # seconds from session start
    end: float
    _session: "Session"

    @property
    def duration(self) -> float:
        return self.end - self.start

    def eeg(self) -> pd.DataFrame:
        df = self._session.eeg()
        return df[(df.t >= self.start) & (df.t < self.end)]

    def __repr__(self) -> str:
        return (f"Trial({self.index}, {self.eyes}/{self.task}, "
                f"{self.start:.1f}-{self.end:.1f}s)")


class Session:
    def __init__(self, directory: str | Path):
        self.dir = Path(directory)
        if not self.dir.is_dir():
            raise FileNotFoundError(f"{self.dir} is not a directory")
        with open(self.dir / "meta.json") as f:
            self.meta = json.load(f)
        self._cache: dict[str, pd.DataFrame] = {}

    @property
    def label(self) -> str:
        return self.meta.get("label", "")

    @property
    def duration(self) -> float:
        return float(self.meta.get("duration_s") or 0.0)

    def _read(self, name: str) -> pd.DataFrame:
        if name not in self._cache:
            path = _open(self.dir / name)
            opener = gzip.open if path.suffix == ".gz" else open
            with opener(path, "rt") as f:
                self._cache[name] = pd.read_csv(f)
        return self._cache[name]

    def eeg(self) -> pd.DataFrame:
        """Raw EEG in microvolts, 256 Hz, one row per sample."""
        return self._read("eeg.csv")

    def ppg(self) -> pd.DataFrame:
        """Pulse, 64 Hz. Muse 2 (MU-03) only — absent on the 2016 Muse."""
        return self._read("ppg.csv")

    def acc(self) -> pd.DataFrame:
        return self._read("acc.csv")

    def gyro(self) -> pd.DataFrame:
        return self._read("gyro.csv")

    def bands(self) -> pd.DataFrame:
        """Per-channel band powers at 4 Hz, as computed live in the browser."""
        return self._read("bands.csv")

    def telemetry(self) -> pd.DataFrame:
        """The derived focus score the live display used, 30 Hz."""
        return self._read("telemetry.csv")

    def cues(self) -> pd.DataFrame:
        """Protocol markers. Empty for sessions recorded before the cued protocol."""
        try:
            return self._read("cues.csv")
        except FileNotFoundError:
            return pd.DataFrame(columns=["t", "seat", "phase", "eyes", "task", "trial"])

    def gaps(self) -> pd.DataFrame:
        """Points where the phone reported losing samples. Empty is good."""
        try:
            return self._read("gaps.csv")
        except FileNotFoundError:
            return pd.DataFrame(columns=["t", "seat", "stream", "resumed_at"])

    def trials(self) -> list[Trial]:
        """Cued trials with their condition, in order. Empty if uncued."""
        cues = self.cues()
        if cues.empty:
            return []
        out: list[Trial] = []
        rows = cues.sort_values("t").reset_index(drop=True)
        for i, row in rows.iterrows():
            if row["phase"] != "trial":
                continue
            later = rows[rows.t > row["t"]]
            end = float(later.t.iloc[0]) if len(later) else self.duration
            out.append(Trial(
                index=int(row["trial"]) if str(row["trial"]).strip() else len(out) + 1,
                eyes=str(row["eyes"]), task=str(row["task"]),
                start=float(row["t"]), end=end, _session=self,
            ))
        return out

    def clean_eeg(self, channels: list[str] | None = None,
                  notch_hz: float = 50.0) -> pd.DataFrame:
        """EEG with mains hum notched out and band-limited to 1-45 Hz.

        Worth doing before anything else: poorly-seated ear electrodes pick up
        mains hum orders of magnitude above the brain signal, and it sits
        outside every band of interest so removing it costs nothing.
        """
        from scipy import signal as sig

        df = self.eeg().copy()
        chans = channels or EEG_CHANNELS
        b, a = sig.iirnotch(notch_hz, Q=30, fs=FS)
        sos = sig.butter(4, [1, 45], btype="band", fs=FS, output="sos")
        for c in chans:
            x = sig.detrend(df[c].to_numpy())
            x = sig.filtfilt(b, a, x)
            df[c] = sig.sosfiltfilt(sos, x)
        return df

    def mains_ratio(self, channel: str) -> float:
        """Mains power over the broadband floor. Above ~100 the channel is hum."""
        from scipy import signal as sig

        x = sig.detrend(self.eeg()[channel].to_numpy())
        f, p = sig.welch(x, FS, nperseg=1024)
        floor = float(np.median(p[(f >= 30) & (f <= 45)]))
        if floor <= 0:
            return 1.0
        peak = max(p[(f >= hz - 1) & (f <= hz + 1)].max() for hz in (50, 60))
        return float(peak / floor)

    def __repr__(self) -> str:
        return f"Session({self.label!r}, {self.duration:.0f}s, {len(self.trials())} trials)"
