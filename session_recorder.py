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
    #
    # The /pwa/ entries are not real OSC addresses. They carry telemetry from
    # the browser clients, which send already-computed scores rather than raw
    # EEG, and the prefix keeps them honestly distinct from anything a headset
    # actually emits.
    FIXED = {
        "/muse/eeg":  ("eeg.csv",  ["TP9", "AF7", "AF8", "TP10"]),
        "/muse/gyro": ("gyro.csv", ["x", "y", "z"]),
        "/muse/acc":  ("acc.csv",  ["x", "y", "z"]),
        "/muse/ppg":  ("ppg.csv",  ["ppg1", "ppg2", "ppg3"]),

        "/pwa/telemetry":   ("telemetry.csv",   ["seat", "focus_score", "normalized", "calibrating"]),
        "/pwa/calibration": ("calibration.csv", ["seat", "baseline", "half_range"]),
        "/pwa/seat":        ("seats.csv",       ["seat", "event"]),

        # Raw signal straight off the headset, one row per sample.
        "/pwa/eeg":   ("eeg.csv",   ["seat", "TP9", "AF7", "AF8", "TP10"]),
        "/pwa/ppg":   ("ppg.csv",   ["seat", "ppg1", "ppg2", "ppg3"]),
        "/pwa/acc":   ("acc.csv",   ["seat", "x", "y", "z"]),
        "/pwa/gyro":  ("gyro.csv",  ["seat", "x", "y", "z"]),
        "/pwa/bands": ("bands.csv", ["seat", "channel", "delta", "theta", "alpha", "beta", "gamma"]),
        # Discontinuities where the client reported losing samples. Without this
        # a dropout leaves rows that look perfectly continuous.
        "/pwa/gap":   ("gaps.csv",  ["seat", "stream", "resumed_at"]),
    }

    # Raw capture writes ~1100 rows/sec across three seats. Line buffering would
    # mean a write syscall per row; a real buffer plus periodic flush costs a
    # couple of seconds of data on a crash instead of a syscall per sample.
    WRITE_BUFFER_BYTES = 1 << 16

    # Longest a row may sit in the buffer before reaching the OS. Applied inside
    # record() so every caller gets the bound, not just those that flush.
    AUTO_FLUSH_S = 2.0

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
        self._extra_meta = {}
        self._last_flush = 0.0

    @staticmethod
    def _safe(label):
        cleaned = "".join(c if c.isalnum() or c in "-_" else "_" for c in label)
        return cleaned or "muse"

    def start(self, label, subject="", extra_meta=None):
        with self._lock:
            if self.active:
                return self._session_dir
            self._extra_meta = dict(extra_meta or {})
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
            self._last_flush = self._clock()
            self.active = True
            self._write_meta("recording")
            return self._session_dir

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
                self._flush_if_due()
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
        if addr.startswith("/muse/elements/"):
            vals = [str(a) for a in args[:4]]
            vals += [""] * (4 - len(vals))
            return "elements.csv", ["addr", "v0", "v1", "v2", "v3"], [addr] + vals
        return "other.csv", ["addr", "values"], [addr, "|".join(str(a) for a in args)]

    @property
    def session_dir(self):
        return self._session_dir

    def elapsed(self):
        """Seconds since the session started, on the same clock `record` stamps.

        Lets a caller time-stamp samples itself, which batched raw capture needs:
        the samples in a batch did not all arrive at the moment it was received.
        """
        with self._lock:
            if not self.active:
                return 0.0
            return self._clock() - self._t0

    def flush(self):
        """Push buffered rows to the OS."""
        with self._lock:
            self._flush_unlocked()

    def _flush_unlocked(self):
        """Flush without taking the lock. Callers must already hold it."""
        for f in self._files.values():
            try:
                f.flush()
            except Exception:
                self._errors += 1
        self._last_flush = self._clock()

    def _flush_if_due(self):
        """Bound how much buffered data an unclean shutdown can cost.

        Buffered writes are worth ~1100 syscalls/sec at raw capture rates, but
        they must not depend on callers remembering to flush: the Qt visualizer
        and the neurofeedback view never do, so a segfault or a dying battery
        would silently lose up to a full buffer — minutes of the low-rate marker
        streams, which is exactly the data needed to interpret a session.
        """
        if (self._clock() - self._last_flush) >= self.AUTO_FLUSH_S:
            self._flush_unlocked()

    def _writer_for(self, fname, cols):
        writer = self._writers.get(fname)
        if writer is None:
            f = open(
                self._session_dir / fname, "w", newline="", buffering=self.WRITE_BUFFER_BYTES
            )
            self._files[fname] = f
            writer = csv.writer(f)
            writer.writerow(["t"] + cols)
            self._writers[fname] = writer
            self._counts.setdefault(fname, 0)
            self._columns[fname] = cols
        return writer

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
        # Caller-supplied keys take precedence; only pass non-standard keys
        # (e.g. mode/calibration/protocol) to avoid clobbering the fields above.
        meta.update(self._extra_meta)
        with open(self._session_dir / "meta.json", "w") as f:
            json.dump(meta, f, indent=2)
