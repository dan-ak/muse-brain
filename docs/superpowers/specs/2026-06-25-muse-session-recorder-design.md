# Muse 2 Session Recorder — Design

**Date:** 2026-06-25
**Status:** Approved, pending implementation

## Problem

The visualizer (`muse_visualizer.py`) receives a Muse 2 OSC stream from the Mind
Monitor phone app, filters and renders it live, then discards everything. The
user wants to **record activity from two different Muse 2 headsets and save it so
work can be built on later** (analysis, comparison, datasets).

The user has **one phone**, so the two headsets are recorded **sequentially** —
one device at a time — and each session is saved separately with a label that
identifies which headset/person it belongs to. No simultaneous multi-device
capture is required.

## Goals

- Capture **everything raw and faithfully**: raw 4-channel EEG @256 Hz
  (TP9/AF7/AF8/TP10, *not* averaged), gyro, accelerometer, PPG, Mind Monitor's
  pre-computed band powers, and any other OSC stream — each timestamped.
- Save in an **analysis-friendly format**: one tidy CSV per stream, loadable in
  a single line with pandas/Excel/MNE, plus a metadata sidecar.
- Control recording **from inside the visualizer** via an `r` hotkey, so the user
  can watch signal quality before and during capture.
- Label each session so sequential recordings accumulate into a dataset.

## Non-Goals (YAGNI)

- Simultaneous two-device capture (multi-port OSC multiplexing).
- Playback of recordings back into the visualizer.
- Format converters (EDF / Parquet / etc.).
- Live side-by-side comparison of two subjects.

The raw CSVs keep all of these buildable later without rework.

## Architecture

### New module: `session_recorder.py`

A self-contained `SessionRecorder` class with **no GUI or network dependency**,
so it is fully unit-testable.

```
SessionRecorder(base_dir="recordings")
  .start(label, subject) -> session_dir
      creates recordings/<YYYYMMDD-HHMMSS>_<label>/
      writes meta.json with status="recording", start_iso, label, subject
      prepares (lazily-opened) CSV writers
  .record(addr, args, t)
      routes one OSC message to the correct CSV (opens the file on first use)
      t = seconds since session start (high-resolution monotonic)
  .stop() -> session_dir
      flushes + closes all files
      rewrites meta.json with status="complete", end_iso, duration_s,
        rows-per-stream counts, sorted list of addresses seen
  .active -> bool
```

**Thread-safety.** OSC handlers run on the server thread; `start`/`stop` fire
from the GUI thread. A single `threading.Lock` guards state transitions and
writes. Critical sections stay small (one CSV row).

**Robustness.** Each `record` call is wrapped in `try/except` so one malformed
packet cannot kill the capture (errors are counted, not raised). Files are
line-buffered so an app crash preserves all flushed rows. `meta.json` is written
at `start` (status `recording`) and rewritten at `stop` (status `complete`), so a
crashed session still has metadata identifying it.

**`stop()` is idempotent** — calling it when not active is a no-op.

### Stream routing

| OSC address                 | File           | Columns                       |
|-----------------------------|----------------|-------------------------------|
| `/muse/eeg`                 | `eeg.csv`      | `t,TP9,AF7,AF8,TP10`          |
| `/muse/gyro`                | `gyro.csv`     | `t,x,y,z`                     |
| `/muse/acc`                 | `acc.csv`      | `t,x,y,z`                     |
| `/muse/ppg`                 | `ppg.csv`      | `t,ppg1,ppg2,ppg3`            |
| `/muse/elements/*`          | `elements.csv` | `t,addr,v0,v1,v2,v3`          |
| anything else               | `other.csv`    | `t,addr,arg0,arg1,...`        |

- Fixed-column files for the well-known fixed-shape streams.
- `elements.csv` is long-format because Mind Monitor emits ~15 different
  `/muse/elements/*` addresses that all share the 4-channel shape.
- `other.csv` is the catch-all (blink/jaw markers, battery, drlref, signal
  quality, and anything unmapped) — **nothing is ever silently dropped**, which
  is what makes "everything raw" actually true.
- **Variable arity:** the column count for a fixed-shape stream is locked from
  the first message of the session; extra values (e.g. a 5th AUX EEG channel) are
  appended as `v4,v5,...`; missing values are left blank. This is recorded in
  `meta.json`.

### Changes to `muse_visualizer.py`

**`OSCReceiver`**
- Holds an optional `self.recorder` reference.
- Every handler — *and the currently-empty `_on_default`* — forwards
  `(addr, raw_args, t)` to the recorder when one is active, **before** any
  viz-specific transformation. The recorder therefore stores the **raw
  4-channel EEG**, independent of the averaging the display does.
- Changing `_on_default` from a no-op to a recorder forward is what captures
  acc/ppg/markers/etc. that the viz currently ignores.

**`MuseDashboard`**
- An `r` `QShortcut` toggles recording (same pattern as the existing `h` head-
  style cycler).
- A small editable **label** `QLineEdit` (seeded from `--label`) and **subject**
  `QLineEdit` placed near the status bar.
- A red **`REC ● mm:ss · N samples`** indicator shown while recording; hidden /
  greyed when idle.
- On stop, the status line briefly shows the saved session folder path.

**`main`**
- New flags: `--label` (default `muse`), `--subject` (default empty),
  `--rec-dir` (default `./recordings`).

### Supporting changes

- **`fake_muse.py`**: also emit `/muse/acc` and a couple of `/muse/elements/*`
  streams so recording can be smoke-tested end-to-end with no headset.
- **`.gitignore`**: add `recordings/` — personal EEG data stays out of version
  control.

## Data Flow

```
Mind Monitor (phone)
   │  OSC/UDP :5000
   ▼
OSCReceiver (server thread)
   ├─ viz path: average EEG, update buffers/gyro/bands  (unchanged)
   └─ if recorder.active: recorder.record(addr, raw_args, t)
                                   │
                                   ▼
                          SessionRecorder
                                   │  one CSV row per message
                                   ▼
                 recordings/<ts>_<label>/{eeg,gyro,acc,...}.csv + meta.json
```

GUI thread: `r` hotkey → `recorder.start(label, subject)` / `recorder.stop()`.

## Error Handling

- `start` failure (cannot create directory): surfaced in the status line; the app
  stays idle, no partial session.
- Per-message write errors: caught and counted, never raised into the OSC thread.
- `stop` is idempotent and always attempts to flush + close every open file.
- A crash mid-session leaves valid CSVs (line-buffered) and a `recording`-status
  `meta.json` identifying the partial session.

## Testing

- **Unit tests** (`test_session_recorder.py`) — the core, fully headless:
  - Feed synthetic OSC messages for each stream; assert exact CSV contents,
    headers, and row ordering.
  - Assert `meta.json` fields (status transitions, counts, addresses, duration).
  - Unmapped address → lands in `other.csv`.
  - Variable arity (5-channel EEG) handled per the locked-column rule.
  - `stop` idempotency; `record` while inactive is a no-op.
- **Smoke test**: run `muse_visualizer.py` against the extended `fake_muse.py`,
  press `r`, confirm a session folder appears with populated CSVs and a
  `complete` `meta.json`.

## File Summary

| File                     | Change                                              |
|--------------------------|-----------------------------------------------------|
| `session_recorder.py`    | new — `SessionRecorder` class                       |
| `test_session_recorder.py` | new — unit tests                                  |
| `muse_visualizer.py`     | recorder hook in `OSCReceiver`; hotkey/UI/flags     |
| `fake_muse.py`           | emit acc + elements for smoke-testing               |
| `.gitignore`             | add `recordings/`                                   |
