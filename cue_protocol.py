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
