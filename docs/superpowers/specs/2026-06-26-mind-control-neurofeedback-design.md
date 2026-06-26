# Mind-Control Neurofeedback Demo — Design

**Date:** 2026-06-26
**Status:** Approved
**Project:** brain (Muse 2 EEG visualizer)

## Goal

A proven-in-the-field neurofeedback "game": a 1-D horizontal bar with a LEFT
(relax) target and a RIGHT (focus) target, and a cursor the user drives with
their mental state. Sustaining focus drifts the cursor right; relaxing / letting
go drifts it left. Once the user can control it, a cued-trial recording mode logs
what they were *cued* to do alongside what their brain and the cursor actually
did — producing a clean, labeled dataset to build on.

## Why this paradigm

The Muse 2 has frontal/temporal electrodes (TP9, AF7, AF8, TP10) and no motor
cortex coverage, so true motor-imagery BCI does not work well on it. The
established, robust paradigm for this headset is **single-metric band-power
neurofeedback**: drive one on-screen indicator from one continuous brain-rhythm
metric whose extremes correspond to "focus" and "relax." This is the approach
Muse's own apps and most consumer neurofeedback demos use.

The chosen metric is the **theta/beta ratio (TBR)**, a standard attention index.
It is genuinely volitional with eyes open and is *already computed* by the
existing visualizer (`_update_status` in `muse_visualizer.py`).

## Decisions (locked during brainstorming)

| Question | Decision |
|----------|----------|
| Control signal | Theta/beta ratio. Focus → cursor RIGHT, relax → cursor LEFT. |
| Control feel | Velocity / accumulator (sustain a state to drive the cursor). |
| Calibration | Guided calibration (relax phase → focus phase) + recenter/recalibrate hotkeys. |
| Recording | Structured cued trials (randomized FOCUS/RELAX with REST intervals). |
| Where it lives | New view inside the existing app, reusing `OSCReceiver` + `SessionRecorder`. |

## Control signal and math

- **Source:** `receiver.latest_band_powers()` returns `(theta_log, beta_log)` —
  Mind Monitor's pre-computed `theta_absolute` / `beta_absolute` (already in
  log10 space). This is non-destructive (unlike `drain_eeg()`, which clears the
  buffer the dashboard consumes), so both views can share one receiver. Until
  `elements` messages arrive the signal is NaN and the cursor freezes with a
  "waiting for signal…" overlay. (The dashboard's FilterBank power fallback is
  intentionally *not* reused here: it would mean draining the same EEG buffer
  the always-running dashboard timer already drains.)
- **Focus signal:** `r = beta_log − theta_log` (higher = more focused). This is
  `−log10(TBR)`, kept in log space because it is symmetric and well-behaved.
- **Smoothing:** exponential moving average of `r` with ~0.7 s time constant to
  remove jitter before it drives the cursor.
- **Calibration → normalization:**
  - `baseline = (relax_mean + focus_mean) / 2` (the neutral midpoint)
  - `half_range = (focus_mean − relax_mean) / 2`
  - drive `d = clamp((r − baseline) / half_range, −1, +1)`
  - `d = +1` at the user's full-focus level, `d = −1` at full-relax.
- **Velocity dynamics:** per frame, `x += (k·d − λ·x)·dt`, with cursor position
  `x` clamped to `[−1, +1]`.
  - `k` = gain (default tuned so sustained max drive crosses the bar in ~3–4 s).
  - `λ` = gentle leak (~10 s time constant) so positions self-correct and the
    user must actively *sustain* a state to hold an edge — the skill being
    trained.
  - `k`, `λ`, and the EMA time constant are configurable.
- **Targets:** left zone `x < −0.8`, right zone `x > +0.8` (configurable). A
  "hit" is registered when the cursor dwells in the cued target zone for ≥ a
  configurable dwell time (default 1.0 s).

## Components

New modules are pure and isolated where possible, mirroring how
`session_recorder.py` was built (GUI/network-agnostic, fully unit-testable).

### `neurofeedback.py` (pure logic, unit-tested)
Responsibilities:
- Compute the focus signal from band powers (with fallback).
- EMA smoothing.
- Calibration: accumulate relax-phase and focus-phase samples, derive
  `baseline` and `half_range`; guard the degenerate case where
  `focus_mean ≈ relax_mean`.
- Velocity-accumulator cursor update (`k·d − λ·x`), clamping, recenter.

Dependencies: numpy only. No Qt, no sockets, no clock — `dt` is passed in.

### `cue_protocol.py` (pure state machine, unit-tested)
Responsibilities:
- Build a balanced, **seeded-random** sequence of FOCUS and RELAX trials, each
  bracketed by REST intervals.
- Given an injected clock, report current phase (`rest` | `cue`), active cue
  (`focus` | `relax` | `None`), time remaining, and trial index.
- Detect per-trial target-dwell hits and produce a per-trial summary.

Dependencies: standard library only. Clock and RNG seed are injected for
deterministic tests.

### `neurofeedback_view.py` (Qt view)
Responsibilities:
- Render the bar, cursor, two target zones, large cue text
  (`FOCUS →` / `RELAX ←` / `REST`), a countdown, and the calibration overlay.
- Each frame: read the latest metric from the receiver, advance the
  `neurofeedback` cursor, advance the `cue_protocol` (when a session is running),
  write to the `TrialLogger`, and repaint.
- Layout kept clean for the OBS virtual-camera output this project targets.

Dependencies: PyQt5/pyqtgraph, `neurofeedback`, `cue_protocol`, `TrialLogger`,
the shared `OSCReceiver` and `SessionRecorder`.

### `muse_visualizer.py` (minimal change)
- Wrap the central area in a `QStackedWidget`: page 0 = existing dashboard,
  page 1 = neurofeedback view.
- Hotkey `n` toggles between them.
- The neurofeedback view receives the *same* `OSCReceiver` and `SessionRecorder`
  instances — no duplicated OSC plumbing.

## Recording — "what I'm trying to do"

A cued session reuses `SessionRecorder`, so **all raw OSC is still captured
faithfully** (eeg.csv, gyro.csv, acc.csv, ppg.csv, elements.csv, other.csv) in
the labeled session folder. Two derived files are written into the *same* folder
by a small `TrialLogger`:

- **`cues.csv`** — one row per trial: `t_start, t_end, cue, goal_side, hit, dwell_s`
- **`feedback.csv`** — per frame: `t, raw_signal, smoothed, drive_d, cursor_x, active_cue`

`meta.json` gains `mode: "neurofeedback"`, the calibration values
(`baseline`, `half_range`), and protocol parameters. This is supplied via a new
optional `extra_meta` keyword argument on `SessionRecorder.start(...)`; the
default (no `extra_meta`) preserves current behavior for the dashboard's plain
recordings.

`TrialLogger` writes into `recorder.session_dir` and is flushed/closed alongside
the recorder. `SessionRecorder` continues to own the folder and `meta.json`.

## Interaction / hotkeys (neurofeedback view)

- `c` — run guided calibration (≈20 s "relax", then ≈20 s "focus").
- `0` — recenter the cursor.
- `t` — start/stop a cued-trial session (auto-starts the recorder, the cue
  protocol, and the logger; auto-stops when all trials finish).
- `n` — return to the dashboard.
- Viewing the cursor with no active session = free practice (no recording).
- Reuses the existing `--label` / `--subject` fields for the session folder.

## Error handling

- **No band data yet:** focus signal is NaN → cursor frozen at center, overlay
  "waiting for signal…".
- **Degenerate calibration** (`half_range` below an epsilon): guard the
  division, keep the prior gain, and warn "weak calibration, try again".
- **Window close mid-session:** `SessionRecorder` already finalizes `meta.json`
  and closes files; `TrialLogger` is flushed/closed in the same path.

## Testing

- `neurofeedback.py`: focus-signal computation (incl. fallback), EMA smoothing,
  calibration (normal and degenerate), velocity integration / clamp / leak with
  injected `dt` and signal, recenter.
- `cue_protocol.py`: balanced seeded sequence generation, timed phase
  transitions via an injected clock, hit detection.
- `TrialLogger`: `cues.csv` and `feedback.csv` written with the correct columns.
- Headless offscreen-Qt smoke test of the view wiring (as done for the recorder).

## Out of scope (YAGNI)

- Free-play / manual-label recording mode (only structured cued trials were
  chosen).
- Motor-imagery or multi-class BCI.
- Real-time model training / classification (the dataset is for building on
  *later*).
- Simultaneous multi-device capture.
