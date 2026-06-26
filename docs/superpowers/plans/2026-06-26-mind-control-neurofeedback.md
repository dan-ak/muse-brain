# Mind-Control Neurofeedback Demo Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a 1-D neurofeedback "game" view to the Muse 2 visualizer where the user drives a left/right cursor with focus vs. relax (theta/beta ratio), then records cued trials labeled with what they were asked to do.

**Architecture:** Three pure, unit-tested modules — `neurofeedback.py` (focus signal, calibration, velocity-accumulator cursor), `cue_protocol.py` (seeded cued-trial state machine + dwell hits), `trial_logger.py` (writes `cues.csv`/`feedback.csv` into a session folder) — plus a Qt view `neurofeedback_view.py` that wires them to the existing `OSCReceiver` and `SessionRecorder`. `muse_visualizer.py` gains a `QStackedWidget` (hotkey `n` toggles dashboard ↔ neurofeedback) and forwards `c`/`0`/`t` to the view. `SessionRecorder.start` gets an `extra_meta` kwarg so `meta.json` records calibration + protocol params.

**Tech Stack:** Python 3, PyQt5/pyqtgraph, numpy, python-osc, pytest. Run tests with `.venv/bin/python -m pytest`.

---

## File Structure

| File | Responsibility |
|------|----------------|
| `neurofeedback.py` (new) | `focus_signal_from_bands`, `ema`, `Calibrator`, `FocusCursor`. Pure math, no Qt/sockets/clock. |
| `cue_protocol.py` (new) | `Trial`, `build_sequence`, `SessionState`, `TrialResult`, `CueSession`. Pure, injected clock. |
| `trial_logger.py` (new) | `TrialLogger` — writes `feedback.csv` + `cues.csv` into a recording folder. |
| `neurofeedback_view.py` (new) | `NeurofeedbackView(QWidget)` — render bar/cursor/targets/cue, per-frame loop, calibration + session lifecycle. |
| `session_recorder.py` (modify) | `start(..., extra_meta=None)`; merge `extra_meta` into `meta.json`. |
| `muse_visualizer.py` (modify) | `QStackedWidget` + `n`/`c`/`0`/`t` shortcuts; guard `r`; `main()` shutdown + CLI flags. |
| `test_neurofeedback.py` (new) | Unit tests for `neurofeedback.py`. |
| `test_cue_protocol.py` (new) | Unit tests for `cue_protocol.py`. |
| `test_trial_logger.py` (new) | Unit tests for `trial_logger.py`. |
| `test_neurofeedback_view.py` (new) | Offscreen-Qt smoke test for the view + integration. |
| `test_session_recorder.py` (modify) | One added test for `extra_meta`. |

Constants live in `neurofeedback_view.py`: `REST_S = 5.0`, `CUE_S = 10.0`, `TARGET = 0.8`, `DWELL = 1.0`, `RELAX_SECS = 20.0`, `FOCUS_SECS = 20.0`.

---

## Task 1: Focus signal + EMA helper

**Files:**
- Create: `neurofeedback.py`
- Test: `test_neurofeedback.py`

- [ ] **Step 1: Write the failing test**

Create `test_neurofeedback.py`:

```python
import math

from neurofeedback import focus_signal_from_bands, ema


def test_focus_signal_is_beta_minus_theta():
    # Higher beta relative to theta => more focused => larger signal.
    assert focus_signal_from_bands(theta_log=0.2, beta_log=0.5) == 0.3


def test_focus_signal_is_nan_when_bands_missing():
    assert math.isnan(focus_signal_from_bands(float("nan"), 0.5))
    assert math.isnan(focus_signal_from_bands(0.2, float("nan")))


def test_ema_seeds_on_first_nan_prev():
    # With no prior value, EMA adopts the new sample.
    assert ema(float("nan"), 3.0, dt=0.1, tau=0.7) == 3.0


def test_ema_zero_tau_passes_through():
    assert ema(1.0, 5.0, dt=0.1, tau=0.0) == 5.0


def test_ema_moves_toward_new_value():
    out = ema(0.0, 1.0, dt=0.7, tau=0.7)  # dt == tau => alpha = 1 - 1/e
    assert math.isclose(out, 1.0 - math.exp(-1.0), rel_tol=1e-9)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest test_neurofeedback.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'neurofeedback'`

- [ ] **Step 3: Write minimal implementation**

Create `neurofeedback.py`:

```python
"""Pure neurofeedback logic: focus signal, calibration, and cursor dynamics.

No Qt, no sockets, no wall clock — dt is passed in by the caller — so every
piece here is deterministic and unit-testable.
"""

from __future__ import annotations

import math


def focus_signal_from_bands(theta_log: float, beta_log: float) -> float:
    """Focus signal from Mind Monitor's log10 band powers.

    Returns ``beta_log - theta_log`` (higher = more focused). Returns NaN if
    either input is NaN, i.e. no band data has arrived yet.
    """
    if math.isnan(theta_log) or math.isnan(beta_log):
        return float("nan")
    return beta_log - theta_log


def ema(prev: float, new: float, dt: float, tau: float) -> float:
    """One exponential-moving-average step with time constant ``tau`` seconds.

    A NaN ``prev`` (no history yet) or non-positive ``tau`` adopts ``new``.
    """
    if math.isnan(prev) or tau <= 0.0:
        return new
    alpha = 1.0 - math.exp(-dt / tau)
    return prev + alpha * (new - prev)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest test_neurofeedback.py -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
git add neurofeedback.py test_neurofeedback.py
git commit -m "feat: focus signal + EMA helper for neurofeedback"
```

---

## Task 2: Calibrator

**Files:**
- Modify: `neurofeedback.py`
- Test: `test_neurofeedback.py`

- [ ] **Step 1: Write the failing test**

Append to `test_neurofeedback.py`:

```python
from neurofeedback import Calibrator


def test_calibrator_midpoint_and_half_range():
    cal = Calibrator()
    for r in (0.0, 1.0):       # relax mean = 0.5
        cal.add_relax(r)
    for r in (3.0, 5.0):       # focus mean = 4.0
        cal.add_focus(r)
    baseline, half_range, ok = cal.result()
    assert ok is True
    assert baseline == 2.25     # midpoint of 0.5 and 4.0
    assert half_range == 1.75   # half of (4.0 - 0.5)


def test_calibrator_degenerate_when_states_too_close():
    cal = Calibrator()
    cal.add_relax(1.0)
    cal.add_focus(1.0)          # focus == relax => no usable range
    baseline, half_range, ok = cal.result()
    assert ok is False
    assert half_range == 1.0    # safe non-zero fallback


def test_calibrator_needs_both_phases():
    cal = Calibrator()
    cal.add_relax(1.0)
    assert cal.result()[2] is False


def test_calibrator_ignores_nan_samples():
    cal = Calibrator()
    cal.add_relax(float("nan"))
    cal.add_relax(0.0)
    cal.add_focus(4.0)
    baseline, half_range, ok = cal.result()
    assert ok is True
    assert baseline == 2.0      # midpoint of 0.0 and 4.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest test_neurofeedback.py -k calibrator -v`
Expected: FAIL with `ImportError: cannot import name 'Calibrator'`

- [ ] **Step 3: Write minimal implementation**

Append to `neurofeedback.py`:

```python
class Calibrator:
    """Collects relax-phase and focus-phase focus-signal samples and derives the
    neutral baseline and half-range used to normalize the cursor drive."""

    def __init__(self):
        self._relax: list[float] = []
        self._focus: list[float] = []

    def add_relax(self, r: float) -> None:
        if not math.isnan(r):
            self._relax.append(r)

    def add_focus(self, r: float) -> None:
        if not math.isnan(r):
            self._focus.append(r)

    def result(self, min_range: float = 1e-3) -> tuple[float, float, bool]:
        """Return ``(baseline, half_range, ok)``.

        ``baseline`` is the midpoint of the relax and focus means; ``half_range``
        is half their difference. ``ok`` is False when a phase has no samples or
        the two states are too close to tell apart (degenerate calibration), in
        which case ``half_range`` is a safe 1.0.
        """
        if not self._relax or not self._focus:
            return 0.0, 1.0, False
        relax_mean = sum(self._relax) / len(self._relax)
        focus_mean = sum(self._focus) / len(self._focus)
        baseline = 0.5 * (relax_mean + focus_mean)
        half_range = 0.5 * (focus_mean - relax_mean)
        if half_range < min_range:
            return baseline, 1.0, False
        return baseline, half_range, True
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest test_neurofeedback.py -v`
Expected: PASS (9 passed)

- [ ] **Step 5: Commit**

```bash
git add neurofeedback.py test_neurofeedback.py
git commit -m "feat: Calibrator derives baseline/half-range with degenerate guard"
```

---

## Task 3: FocusCursor (velocity-accumulator dynamics)

**Files:**
- Modify: `neurofeedback.py`
- Test: `test_neurofeedback.py`

- [ ] **Step 1: Write the failing test**

Append to `test_neurofeedback.py`:

```python
from neurofeedback import FocusCursor


def test_cursor_integrates_drive_over_time():
    # tau=0 => smoothed == raw; gain=1, leak=0 => x += drive*dt each step.
    c = FocusCursor(gain=1.0, leak=0.0, tau=0.0, baseline=0.0, half_range=1.0)
    assert c.update(1.0, dt=0.5) == 0.5
    assert c.update(1.0, dt=0.5) == 1.0


def test_cursor_clamps_to_unit_range():
    c = FocusCursor(gain=1.0, leak=0.0, tau=0.0, baseline=0.0, half_range=1.0)
    c.update(1.0, dt=0.5)
    c.update(1.0, dt=0.5)
    assert c.update(1.0, dt=0.5) == 1.0   # would be 1.5, clamped


def test_cursor_drive_is_clamped_unit():
    c = FocusCursor(gain=1.0, leak=0.0, tau=0.0, baseline=0.0, half_range=1.0)
    c.update(10.0, dt=0.1)   # raw far above range
    assert c.drive == 1.0


def test_cursor_leak_pulls_toward_center():
    c = FocusCursor(gain=0.0, leak=1.0, tau=0.0, baseline=0.0, half_range=1.0)
    c.x = 1.0
    c.update(0.0, dt=0.5)    # x += (0 - 1.0*1.0)*0.5
    assert c.x == 0.5


def test_cursor_freezes_on_nan_signal():
    c = FocusCursor(tau=0.0)
    c.x = 0.4
    assert c.update(float("nan"), dt=0.5) == 0.4
    assert c.x == 0.4


def test_cursor_recenter_resets_position():
    c = FocusCursor(tau=0.0)
    c.x = 0.7
    c.recenter()
    assert c.x == 0.0


def test_cursor_set_calibration_guards_zero_range():
    c = FocusCursor()
    c.set_calibration(baseline=2.0, half_range=0.0)
    assert c.baseline == 2.0
    assert c.half_range == 1.0   # zero replaced with safe 1.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest test_neurofeedback.py -k cursor -v`
Expected: FAIL with `ImportError: cannot import name 'FocusCursor'`

- [ ] **Step 3: Write minimal implementation**

Append to `neurofeedback.py`:

```python
class FocusCursor:
    """Velocity-accumulator cursor driven by the (smoothed) focus signal.

    ``update`` EMA-smooths the raw signal, converts it to a drive in [-1, 1]
    using the calibrated baseline/half_range, integrates velocity with a gentle
    self-centering leak, and clamps the position to [-1, 1]. A NaN input freezes
    the cursor (no band data).
    """

    def __init__(self, gain: float = 0.4, leak: float = 0.1, tau: float = 0.7,
                 baseline: float = 0.0, half_range: float = 1.0):
        self.gain = gain          # position units per second at full drive
        self.leak = leak          # self-centering rate (~1/leak seconds)
        self.tau = tau            # EMA smoothing time constant (seconds)
        self.baseline = baseline
        self.half_range = half_range if half_range > 1e-9 else 1.0
        self.x = 0.0              # position in [-1, 1]
        self.smoothed = float("nan")
        self.drive = 0.0

    def set_calibration(self, baseline: float, half_range: float) -> None:
        self.baseline = baseline
        self.half_range = half_range if half_range > 1e-9 else 1.0

    def recenter(self) -> None:
        self.x = 0.0

    def update(self, raw_signal: float, dt: float) -> float:
        if math.isnan(raw_signal):
            return self.x        # freeze: no data
        self.smoothed = ema(self.smoothed, raw_signal, dt, self.tau)
        d = (self.smoothed - self.baseline) / self.half_range
        d = max(-1.0, min(1.0, d))
        self.drive = d
        self.x += (self.gain * d - self.leak * self.x) * dt
        self.x = max(-1.0, min(1.0, self.x))
        return self.x
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest test_neurofeedback.py -v`
Expected: PASS (16 passed)

- [ ] **Step 5: Commit**

```bash
git add neurofeedback.py test_neurofeedback.py
git commit -m "feat: FocusCursor velocity-accumulator with leak, clamp, NaN freeze"
```

---

## Task 4: Cued-trial sequence builder

**Files:**
- Create: `cue_protocol.py`
- Test: `test_cue_protocol.py`

- [ ] **Step 1: Write the failing test**

Create `test_cue_protocol.py`:

```python
from cue_protocol import Trial, build_sequence, FOCUS, RELAX, REST


def test_sequence_brackets_each_cue_with_rest():
    seq = build_sequence(n_cues=4, seed=1, rest_s=5.0, cue_s=10.0)
    kinds = [t.kind for t in seq]
    assert kinds[0] == REST and kinds[-1] == REST   # leading + trailing rest
    assert len(seq) == 2 * 4 + 1                     # rest before each cue + trailing


def test_sequence_is_balanced():
    seq = build_sequence(n_cues=4, seed=1)
    cues = [t.kind for t in seq if t.kind in (FOCUS, RELAX)]
    assert len(cues) == 4
    assert cues.count(FOCUS) == 2
    assert cues.count(RELAX) == 2


def test_sequence_is_seed_deterministic():
    a = [t.kind for t in build_sequence(n_cues=6, seed=7)]
    b = [t.kind for t in build_sequence(n_cues=6, seed=7)]
    assert a == b


def test_cue_trials_carry_goal_side():
    seq = build_sequence(n_cues=2, seed=0)
    for t in seq:
        if t.kind == FOCUS:
            assert t.goal_side == "right"
        elif t.kind == RELAX:
            assert t.goal_side == "left"
        else:
            assert t.goal_side is None


def test_odd_n_cues_rounded_down_to_balance():
    seq = build_sequence(n_cues=5, seed=0)
    cues = [t.kind for t in seq if t.kind in (FOCUS, RELAX)]
    assert len(cues) == 4   # 5 // 2 * 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest test_cue_protocol.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'cue_protocol'`

- [ ] **Step 3: Write minimal implementation**

Create `cue_protocol.py`:

```python
"""Pure cued-trial protocol.

Builds a balanced, seeded-random sequence of FOCUS and RELAX trials bracketed by
REST intervals, and (via CueSession) tracks the active phase, countdown, and
per-trial target-dwell hits against an injected clock. No Qt, no wall clock.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

REST = "rest"
FOCUS = "focus"
RELAX = "relax"

GOAL = {FOCUS: "right", RELAX: "left"}


@dataclass
class Trial:
    kind: str                       # REST | FOCUS | RELAX
    duration: float                 # seconds
    goal_side: str | None = None    # "right" | "left" | None (rest)


def build_sequence(n_cues: int = 8, seed: int = 0,
                   rest_s: float = 5.0, cue_s: float = 10.0) -> list[Trial]:
    """A balanced, seeded-random sequence: a REST before each FOCUS/RELAX cue,
    plus a trailing REST. ``n_cues`` is rounded down to an even number so FOCUS
    and RELAX are balanced."""
    half = n_cues // 2
    cues = [FOCUS] * half + [RELAX] * half
    random.Random(seed).shuffle(cues)
    seq: list[Trial] = []
    for kind in cues:
        seq.append(Trial(REST, rest_s))
        seq.append(Trial(kind, cue_s, GOAL[kind]))
    seq.append(Trial(REST, rest_s))
    return seq
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest test_cue_protocol.py -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
git add cue_protocol.py test_cue_protocol.py
git commit -m "feat: balanced seeded cued-trial sequence builder"
```

---

## Task 5: CueSession phase/timing

**Files:**
- Modify: `cue_protocol.py`
- Test: `test_cue_protocol.py`

- [ ] **Step 1: Write the failing test**

Append to `test_cue_protocol.py`:

```python
from cue_protocol import CueSession, SessionState


class FakeClock:
    def __init__(self, t=0.0):
        self.t = t

    def __call__(self):
        return self.t


def _one_cue_seq():
    # rest(5) -> focus(10) -> rest(5); cumulative ends = [5, 15, 20]
    return [Trial(REST, 5.0), Trial(FOCUS, 10.0, "right"), Trial(REST, 5.0)]


def test_session_reports_leading_rest():
    clk = FakeClock(0.0)
    s = CueSession(_one_cue_seq(), clock_fn=clk)
    st = s.update(0.0)
    assert isinstance(st, SessionState)
    assert st.phase == REST
    assert st.done is False
    assert st.time_remaining == 5.0


def test_session_reports_active_cue_and_countdown():
    clk = FakeClock(0.0)
    s = CueSession(_one_cue_seq(), clock_fn=clk)
    s.update(0.0)
    clk.t = 7.0
    st = s.update(0.0)
    assert st.phase == FOCUS
    assert st.goal_side == "right"
    assert st.time_remaining == 8.0   # ends[1]=15 - elapsed 7
    assert st.cue_number == 1


def test_session_marks_done_after_last_trial():
    clk = FakeClock(0.0)
    s = CueSession(_one_cue_seq(), clock_fn=clk)
    s.update(0.0)
    clk.t = 20.0
    st = s.update(0.0)
    assert st.done is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest test_cue_protocol.py -k session -v`
Expected: FAIL with `ImportError: cannot import name 'CueSession'`

- [ ] **Step 3: Write minimal implementation**

Append to `cue_protocol.py`:

```python
@dataclass
class SessionState:
    phase: str                  # REST | FOCUS | RELAX
    goal_side: str | None
    trial_index: int            # index into the sequence
    cue_number: int             # 1-based count of cue trials reached (0 in leading rest)
    time_remaining: float
    done: bool


@dataclass
class TrialResult:
    cue: str
    goal_side: str
    t_start: float
    t_end: float
    hit: bool
    dwell_s: float


class CueSession:
    """Drives a Trial sequence against an injected clock and records per-trial
    dwell hits. Call ``update(cursor_x)`` once per frame."""

    def __init__(self, sequence: list[Trial], clock_fn,
                 target: float = 0.8, dwell_needed: float = 1.0):
        self._seq = sequence
        self._clock = clock_fn
        self._target = target
        self._dwell_needed = dwell_needed
        self._t0 = clock_fn()
        self._last_t = self._t0
        self._ends: list[float] = []
        acc = 0.0
        for tr in sequence:
            acc += tr.duration
            self._ends.append(acc)
        self._cur_index = -1
        self._dwell = 0.0
        self.results: list[TrialResult] = []

    def _index_at(self, elapsed: float) -> int:
        for i, end in enumerate(self._ends):
            if elapsed < end:
                return i
        return len(self._seq)

    def _cue_count(self, idx: int) -> int:
        upto = min(idx + 1, len(self._seq))
        return sum(1 for tr in self._seq[:upto] if tr.kind in (FOCUS, RELAX))

    def update(self, cursor_x: float) -> SessionState:
        now = self._clock()
        elapsed = now - self._t0
        dt = now - self._last_t
        self._last_t = now
        idx = self._index_at(elapsed)

        if idx != self._cur_index:
            self._finalize(self._cur_index)
            self._cur_index = idx
            self._dwell = 0.0
        elif idx < len(self._seq):
            tr = self._seq[idx]
            if tr.kind in (FOCUS, RELAX) and dt > 0:
                in_zone = (cursor_x >= self._target if tr.goal_side == "right"
                           else cursor_x <= -self._target)
                if in_zone:
                    self._dwell += dt

        return self._state(idx, elapsed)

    def _finalize(self, idx: int) -> None:
        if idx is None or idx < 0 or idx >= len(self._seq):
            return
        tr = self._seq[idx]
        if tr.kind in (FOCUS, RELAX):
            t_end = self._ends[idx]
            self.results.append(TrialResult(
                cue=tr.kind, goal_side=tr.goal_side,
                t_start=t_end - tr.duration, t_end=t_end,
                hit=self._dwell >= self._dwell_needed,
                dwell_s=round(self._dwell, 3)))

    def _state(self, idx: int, elapsed: float) -> SessionState:
        if idx >= len(self._seq):
            return SessionState(REST, None, idx, self._cue_count(idx), 0.0, True)
        tr = self._seq[idx]
        return SessionState(tr.kind, tr.goal_side, idx, self._cue_count(idx),
                            max(0.0, self._ends[idx] - elapsed), False)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest test_cue_protocol.py -v`
Expected: PASS (8 passed)

- [ ] **Step 5: Commit**

```bash
git add cue_protocol.py test_cue_protocol.py
git commit -m "feat: CueSession phase/countdown state machine"
```

---

## Task 6: CueSession dwell-hit detection

**Files:**
- Modify: `cue_protocol.py` (no code change expected — logic already present; this task locks behavior with tests)
- Test: `test_cue_protocol.py`

- [ ] **Step 1: Write the failing test**

Append to `test_cue_protocol.py`:

```python
def test_hit_recorded_when_cursor_dwells_in_goal_zone():
    clk = FakeClock(0.0)
    s = CueSession(_one_cue_seq(), clock_fn=clk, target=0.8, dwell_needed=1.0)
    s.update(0.0)                       # leading rest
    for t in (6.0, 6.5, 7.0, 7.5, 8.0):  # 6.0 transitions in; 4 frames * 0.5s dwell = 2.0s
        clk.t = t
        s.update(0.9)                  # in the right zone
    clk.t = 16.0
    s.update(0.0)                      # transition to trailing rest -> finalize cue
    assert len(s.results) == 1
    assert s.results[0].cue == FOCUS
    assert s.results[0].hit is True
    assert s.results[0].dwell_s == 2.0


def test_miss_recorded_when_cursor_never_reaches_goal():
    clk = FakeClock(0.0)
    s = CueSession(_one_cue_seq(), clock_fn=clk, target=0.8, dwell_needed=1.0)
    s.update(0.0)
    for t in (6.0, 6.5, 7.0, 7.5, 8.0):
        clk.t = t
        s.update(0.0)                  # never in zone
    clk.t = 16.0
    s.update(0.0)
    assert s.results[0].hit is False
    assert s.results[0].dwell_s == 0.0


def test_transition_frame_does_not_count_dwell():
    # The large dt of the rest->cue transition frame must not be counted.
    clk = FakeClock(0.0)
    s = CueSession(_one_cue_seq(), clock_fn=clk, target=0.8, dwell_needed=1.0)
    s.update(0.0)
    clk.t = 6.0
    s.update(0.9)                      # transition into cue: dt=6 but not counted
    clk.t = 16.0
    s.update(0.0)
    assert s.results[0].dwell_s == 0.0
```

- [ ] **Step 2: Run test to verify it fails or passes**

Run: `.venv/bin/python -m pytest test_cue_protocol.py -k "hit or miss or transition" -v`
Expected: PASS (logic was implemented in Task 5). If any FAIL, fix `update`/`_finalize` in `cue_protocol.py` until green — do not change the tests.

- [ ] **Step 3: Commit**

```bash
git add test_cue_protocol.py
git commit -m "test: lock CueSession dwell-hit detection behavior"
```

---

## Task 7: TrialLogger

**Files:**
- Create: `trial_logger.py`
- Test: `test_trial_logger.py`

- [ ] **Step 1: Write the failing test**

Create `test_trial_logger.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest test_trial_logger.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'trial_logger'`

- [ ] **Step 3: Write minimal implementation**

Create `trial_logger.py`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest test_trial_logger.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add trial_logger.py test_trial_logger.py
git commit -m "feat: TrialLogger writes feedback.csv + cues.csv"
```

---

## Task 8: SessionRecorder `extra_meta`

**Files:**
- Modify: `session_recorder.py:27-43` (`__init__`), `session_recorder.py:50-69` (`start`), `session_recorder.py:132-149` (`_write_meta`)
- Test: `test_session_recorder.py`

- [ ] **Step 1: Write the failing test**

Append to `test_session_recorder.py`:

```python
def test_start_merges_extra_meta_into_meta_json(tmp_path):
    rec = make_recorder(tmp_path)
    sd = rec.start("museA", extra_meta={"mode": "neurofeedback",
                                        "baseline": 1.5, "half_range": 0.8})
    rec.stop()
    meta = json.loads((sd / "meta.json").read_text())
    assert meta["mode"] == "neurofeedback"
    assert meta["baseline"] == 1.5
    assert meta["half_range"] == 0.8
    # standard keys still present
    assert meta["label"] == "museA"
    assert meta["status"] == "complete"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest test_session_recorder.py -k extra_meta -v`
Expected: FAIL with `TypeError: start() got an unexpected keyword argument 'extra_meta'`

- [ ] **Step 3: Write the implementation**

In `session_recorder.py`, add to `__init__` (after `self._errors = 0`):

```python
        self._extra_meta = {}
```

Change the `start` signature and add a reset. Replace:

```python
    def start(self, label, subject=""):
        with self._lock:
            if self.active:
                return self._session_dir
            self._start_dt = self._now()
```

with:

```python
    def start(self, label, subject="", extra_meta=None):
        with self._lock:
            if self.active:
                return self._session_dir
            self._extra_meta = dict(extra_meta or {})
            self._start_dt = self._now()
```

In `_write_meta`, immediately before the `with open(...)` line that writes the file, add the merge:

```python
        meta.update(self._extra_meta)
        with open(self._session_dir / "meta.json", "w") as f:
            json.dump(meta, f, indent=2)
```

- [ ] **Step 4: Run the full recorder suite to verify nothing regressed**

Run: `.venv/bin/python -m pytest test_session_recorder.py -v`
Expected: PASS (11 passed — 10 existing + 1 new)

- [ ] **Step 5: Commit**

```bash
git add session_recorder.py test_session_recorder.py
git commit -m "feat: SessionRecorder.start accepts extra_meta for meta.json"
```

---

## Task 9: NeurofeedbackView (Qt) + offscreen smoke test

**Files:**
- Create: `neurofeedback_view.py`
- Test: `test_neurofeedback_view.py`

- [ ] **Step 1: Write the failing test**

Create `test_neurofeedback_view.py`:

```python
import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import time

import pytest
from PyQt5 import QtWidgets

from muse_visualizer import OSCReceiver
from session_recorder import SessionRecorder
from neurofeedback_view import NeurofeedbackView


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _make_view(tmp_path):
    receiver = OSCReceiver(host="127.0.0.1", port=0)   # no thread started
    recorder = SessionRecorder(base_dir=tmp_path)
    view = NeurofeedbackView(receiver, recorder,
                             label_fn=lambda: "nf", subject_fn=lambda: "dan")
    return receiver, recorder, view


def _feed_bands(receiver, theta, beta):
    with receiver.lock:
        receiver.theta_abs = theta
        receiver.beta_abs = beta
        receiver.bands_ts = time.monotonic()


def test_cursor_moves_right_when_focused(app, tmp_path):
    receiver, recorder, view = _make_view(tmp_path)
    try:
        # baseline 0, half_range 1: beta-theta = 1.0 => full positive drive.
        view.cursor.set_calibration(baseline=0.0, half_range=1.0)
        _feed_bands(receiver, theta=0.0, beta=1.0)
        for _ in range(20):
            view._tick()
        assert view.cursor.x > 0.0
    finally:
        receiver.server.server_close()


def test_no_data_keeps_cursor_centered(app, tmp_path):
    receiver, recorder, view = _make_view(tmp_path)
    try:
        for _ in range(10):     # bands_ts == 0 => latest_band_powers is nan/nan
            view._tick()
        assert view.cursor.x == 0.0
    finally:
        receiver.server.server_close()


def test_session_records_raw_and_derived_streams(app, tmp_path):
    receiver, recorder, view = _make_view(tmp_path)
    try:
        _feed_bands(receiver, theta=0.0, beta=1.0)
        view.toggle_session()                 # starts recorder + protocol + logger
        assert recorder.active is True
        session_dir = recorder._session_dir
        for _ in range(5):
            view._tick()                      # writes feedback rows + raw OSC tap
        receiver._on_eeg("/muse/eeg", 1.0, 2.0, 3.0, 4.0)   # raw OSC still captured
        view._stop_session()
        assert recorder.active is False

        assert (session_dir / "feedback.csv").exists()
        assert (session_dir / "eeg.csv").exists()
        meta = json.loads((session_dir / "meta.json").read_text())
        assert meta["mode"] == "neurofeedback"
        assert "protocol" in meta
    finally:
        receiver.server.server_close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest test_neurofeedback_view.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'neurofeedback_view'`

- [ ] **Step 3: Write minimal implementation**

Create `neurofeedback_view.py`:

```python
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
        self.plot.addItem(pg.TextItem("RELAX ◀", color=(150, 190, 255), anchor=(0, 0.5)))
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest test_neurofeedback_view.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add neurofeedback_view.py test_neurofeedback_view.py
git commit -m "feat: NeurofeedbackView with calibration + cued-session lifecycle"
```

---

## Task 10: Wire the view into the dashboard

**Files:**
- Modify: `muse_visualizer.py:419-420` (central widget), `muse_visualizer.py:634-637` (shortcuts), `muse_visualizer.py:644` (`_toggle_record` guard), `muse_visualizer.py:786-811` (`main`)
- Test: `test_neurofeedback_view.py`

- [ ] **Step 1: Write the failing test**

Append to `test_neurofeedback_view.py`:

```python
def test_dashboard_hosts_neurofeedback_view_and_toggles(app, tmp_path):
    from muse_visualizer import MuseDashboard

    receiver = OSCReceiver(host="127.0.0.1", port=0)
    try:
        win = MuseDashboard(receiver, port=0, rec_dir=str(tmp_path),
                            label="museA", subject="dan")
        assert hasattr(win, "nf_view")
        assert win.stack.currentWidget() is not win.nf_view
        win._toggle_view()
        assert win.stack.currentWidget() is win.nf_view
        # 'r' record is a no-op while on the neurofeedback page
        win._toggle_record()
        assert win.recorder.active is False
    finally:
        receiver.server.server_close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest test_neurofeedback_view.py -k dashboard -v`
Expected: FAIL with `AttributeError: 'MuseDashboard' object has no attribute 'nf_view'`

- [ ] **Step 3: Modify `muse_visualizer.py`**

(3a) Add the import after the existing `from session_recorder import SessionRecorder` line (line 26):

```python
from neurofeedback_view import NeurofeedbackView
```

(3b) The central widget is built into `central` and assigned at lines 419-420:

```python
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
```

Change to NOT make it the central widget yet (the stack will, at the end of `__init__`):

```python
        central = QtWidgets.QWidget()
        self._dashboard_page = central
```

(3c) At the END of `__init__`, immediately after the two existing shortcut assignments (lines 634-637), add the stack, the view, and the new shortcuts:

```python
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
```

(3d) `MuseDashboard.__init__` needs `self._nf_cues` / `self._nf_seed`. Change the signature and store them. Replace lines 396-397:

```python
    def __init__(self, receiver: OSCReceiver, port: int,
                 rec_dir: str = "recordings", label: str = "muse", subject: str = ""):
```

with:

```python
    def __init__(self, receiver: OSCReceiver, port: int,
                 rec_dir: str = "recordings", label: str = "muse", subject: str = "",
                 nf_cues: int = 8, nf_seed: int = 0):
```

and immediately after `self._default_subject = subject` (line 404) add:

```python
        self._nf_cues = nf_cues
        self._nf_seed = nf_seed
```

(3e) Add the view-toggle + forwarding handlers. Insert these methods right before `_cycle_head_style` (line 639):

```python
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
```

(3f) Guard `_toggle_record` so `r` only records on the dashboard page. Change the first line of `_toggle_record` (line 644-645):

```python
    def _toggle_record(self):
        if self.recorder.active:
```

to:

```python
    def _toggle_record(self):
        if self.stack.currentWidget() is self.nf_view:
            return  # 'r' records only on the dashboard; use 't' for cued sessions
        if self.recorder.active:
```

(3g) In `main()`, finalize the neurofeedback logger before stopping the recorder. Replace the `finally` block (lines 806-811):

```python
    try:
        rc = app.exec_()
    finally:
        win.recorder.stop()  # finalize meta.json + close files if mid-recording
        receiver.stop()
    sys.exit(rc)
```

with:

```python
    try:
        rc = app.exec_()
    finally:
        win.nf_view.shutdown()   # flush cues/feedback if a cued session is open
        win.recorder.stop()      # finalize meta.json + close files if mid-recording
        receiver.stop()
    sys.exit(rc)
```

(3h) Add CLI flags. After the `--rec-dir` argument (lines 794-795) add:

```python
    parser.add_argument("--nf-trials", type=int, default=8,
                        help="number of cued FOCUS/RELAX trials per session")
    parser.add_argument("--nf-seed", type=int, default=0,
                        help="random seed for cued-trial order")
```

and update the `MuseDashboard(...)` construction (lines 803-804) to pass them:

```python
    win = MuseDashboard(receiver, args.port, rec_dir=args.rec_dir,
                        label=args.label, subject=args.subject,
                        nf_cues=args.nf_trials, nf_seed=args.nf_seed)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv/bin/python -m pytest test_neurofeedback_view.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Verify the module still imports cleanly and `--help` works**

Run: `.venv/bin/python -c "import muse_visualizer"`
Expected: no output, exit 0

Run: `.venv/bin/python muse_visualizer.py --help`
Expected: usage text listing `--nf-trials` and `--nf-seed`

- [ ] **Step 6: Commit**

```bash
git add muse_visualizer.py test_neurofeedback_view.py
git commit -m "feat: neurofeedback view as a togglable page in the dashboard"
```

---

## Task 11: Full-suite verification + end-to-end smoke

**Files:**
- No production code changes (verification only).

- [ ] **Step 1: Run the entire test suite**

Run: `.venv/bin/python -m pytest -v`
Expected: PASS — all tests across `test_neurofeedback.py` (16), `test_cue_protocol.py` (11), `test_trial_logger.py` (4), `test_session_recorder.py` (11), `test_neurofeedback_view.py` (4).

- [ ] **Step 2: End-to-end OSC smoke (no headset)**

Write `/home/dan/.claude/jobs/f82969f5/tmp/nf_smoke.py`:

```python
import os, time, json
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import tempfile
from pathlib import Path
from PyQt5 import QtWidgets
from muse_visualizer import OSCReceiver
from session_recorder import SessionRecorder
from neurofeedback_view import NeurofeedbackView

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
tmp = Path(tempfile.mkdtemp())
receiver = OSCReceiver(host="127.0.0.1", port=0)
recorder = SessionRecorder(base_dir=tmp)
view = NeurofeedbackView(receiver, recorder,
                         label_fn=lambda: "smoke", subject_fn=lambda: "tester",
                         n_cues=2, seed=0)
with receiver.lock:
    receiver.theta_abs, receiver.beta_abs = 0.0, 1.0
    receiver.bands_ts = time.monotonic()
view.cursor.set_calibration(0.0, 1.0)
view.toggle_session()
sd = recorder._session_dir
for _ in range(30):
    view._tick()
    receiver._on_eeg("/muse/eeg", 1.0, 2.0, 3.0, 4.0)
    time.sleep(0.01)
view._stop_session()
receiver.server.server_close()

meta = json.loads((sd / "meta.json").read_text())
print("files:", sorted(p.name for p in sd.iterdir()))
print("mode:", meta["mode"], "status:", meta["status"])
assert (sd / "feedback.csv").exists()
assert (sd / "eeg.csv").exists()
assert meta["mode"] == "neurofeedback"
print("NF SMOKE OK")
```

Run: `PYTHONPATH=/home/dan/Code/brain .venv/bin/python /home/dan/.claude/jobs/f82969f5/tmp/nf_smoke.py`
Expected: prints a file list including `feedback.csv`, `eeg.csv`, `meta.json`, and `NF SMOKE OK`.

- [ ] **Step 3: Finish the branch**

REQUIRED SUB-SKILL: Use superpowers:finishing-a-development-branch to verify tests, then present completion options.

---

## Self-Review

**Spec coverage:**
- Control signal (theta/beta, focus→right) → Task 1 (`focus_signal_from_bands`), Task 9 (view uses `latest_band_powers`). ✓
- Velocity/accumulator with leak → Task 3 (`FocusCursor`). ✓
- Guided calibration + recenter/recalibrate hotkeys → Task 2 (`Calibrator`), Task 9 (`start_calibration`, `recenter`), Task 10 (`c`/`0`). ✓
- Cued trials (balanced, seeded, REST-bracketed) + hit detection → Tasks 4, 5, 6. ✓
- Recording = raw OSC (SessionRecorder) + cues.csv/feedback.csv (TrialLogger) + meta mode/calibration/protocol → Tasks 7, 8, 9. ✓
- Integration as a togglable view sharing receiver+recorder; hotkeys `n`/`c`/`0`/`t`; `r` guarded → Task 10. ✓
- Error handling: NaN freeze (Task 3, 9), degenerate calibration (Task 2, 9), window-close flush (Task 9 `shutdown`, Task 10 `main`). ✓
- Testing across all modules + offscreen smoke → Tasks 1–11. ✓

**Placeholder scan:** No TBD/placeholder steps; every code step shows complete code. ✓

**Type consistency:** `focus_signal_from_bands(theta_log, beta_log)`, `Calibrator.result() -> (baseline, half_range, ok)`, `FocusCursor.update(raw, dt) -> x` with `.drive`/`.smoothed`/`.x`, `build_sequence(n_cues, seed, rest_s, cue_s)`, `CueSession(sequence, clock_fn, target, dwell_needed).update(cursor_x) -> SessionState`, `TrialResult(cue, goal_side, t_start, t_end, hit, dwell_s)`, `TrialLogger.log_feedback/log_trial/close`, `SessionRecorder.start(label, subject, extra_meta)`, `NeurofeedbackView(receiver, recorder, label_fn, subject_fn, n_cues, seed)` — names are consistent across defining and consuming tasks. ✓
