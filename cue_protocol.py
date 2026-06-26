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
