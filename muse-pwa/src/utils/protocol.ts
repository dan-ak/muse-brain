// A 2x2 cued protocol: eyes open/closed crossed with calm/focused.
//
// Pure logic — no audio, no React, no clock of its own — so the sequence and the
// phase transitions can be reasoned about and tested on their own. Mirrors the
// design of cue_protocol.py on the OSC path, widened from two conditions to a
// factorial.

export type Eyes = 'open' | 'closed';
export type Task = 'calm' | 'focus';

export type Condition = { eyes: Eyes; task: Task };

export const CONDITIONS: Condition[] = [
  { eyes: 'open', task: 'calm' },
  { eyes: 'open', task: 'focus' },
  { eyes: 'closed', task: 'calm' },
  { eyes: 'closed', task: 'focus' },
];

export const conditionId = (c: Condition) => `${c.eyes}_${c.task}`;

export const conditionLabel = (c: Condition) =>
  `Eyes ${c.eyes}, ${c.task === 'calm' ? 'calm' : 'focused'}`;

/** What the participant hears and, when their eyes are open, reads. */
export const conditionSpeech = (c: Condition) =>
  c.eyes === 'closed'
    ? `Eyes closed. ${c.task === 'calm' ? 'Relax' : 'Focus'}.`
    : `Eyes open. ${c.task === 'calm' ? 'Relax' : 'Focus'}.`;

export type PhaseKind = 'instruct' | 'trial' | 'rest' | 'done';

export type Step = {
  kind: PhaseKind;
  duration: number;
  condition?: Condition;
  /** 1-based index among trial steps, for progress and for the recording. */
  trialIndex?: number;
};

export type ProtocolOptions = {
  repeats?: number;
  trialSeconds?: number;
  restSeconds?: number;
  instructSeconds?: number;
  seed?: number;
};

export const DEFAULT_OPTIONS: Required<ProtocolOptions> = {
  // Six repeats of four conditions at 20 s is about 13 minutes including rests.
  // Short blocks repeated often beat long blocks: the first analysis had two
  // samples per condition and could not distinguish a weak effect from none.
  repeats: 6,
  trialSeconds: 20,
  restSeconds: 8,
  instructSeconds: 4,
  seed: 1,
};

/** Deterministic RNG so a sequence can be reproduced from its seed alone. */
function mulberry32(seed: number) {
  let a = seed >>> 0;
  return () => {
    a |= 0;
    a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

/** Balanced across conditions, shuffled, and never the same condition twice.
 *
 * Consecutive repeats are avoided because a participant who has just spent 20 s
 * with their eyes closed and is told "eyes closed" again cannot tell whether the
 * trial restarted — and the carry-over makes the two blocks hard to treat as
 * independent samples anyway.
 */
export function buildSequence(options: ProtocolOptions = {}): Step[] {
  const o = { ...DEFAULT_OPTIONS, ...options };
  const rng = mulberry32(o.seed);

  const pool: Condition[] = [];
  for (let i = 0; i < o.repeats; i++) pool.push(...CONDITIONS);

  // Fisher-Yates, then a repair pass for adjacent duplicates.
  for (let i = pool.length - 1; i > 0; i--) {
    const j = Math.floor(rng() * (i + 1));
    [pool[i], pool[j]] = [pool[j], pool[i]];
  }
  for (let i = 1; i < pool.length; i++) {
    if (conditionId(pool[i]) !== conditionId(pool[i - 1])) continue;
    const swap = pool.findIndex(
      (c, j) =>
        j > i &&
        conditionId(c) !== conditionId(pool[i - 1]) &&
        (j + 1 >= pool.length || conditionId(pool[i]) !== conditionId(pool[j + 1])),
    );
    if (swap > 0) [pool[i], pool[swap]] = [pool[swap], pool[i]];
  }

  const steps: Step[] = [];
  pool.forEach((condition, index) => {
    steps.push({ kind: 'instruct', duration: o.instructSeconds, condition, trialIndex: index + 1 });
    steps.push({ kind: 'trial', duration: o.trialSeconds, condition, trialIndex: index + 1 });
    steps.push({ kind: 'rest', duration: o.restSeconds });
  });
  steps.push({ kind: 'done', duration: 0 });
  return steps;
}

export const sequenceDuration = (steps: Step[]) =>
  steps.reduce((total, s) => total + s.duration, 0);

export type ProtocolState = {
  step: Step;
  stepIndex: number;
  /** Seconds remaining in the current step. */
  remaining: number;
  /** Completed trials over total trials. */
  trialsDone: number;
  trialsTotal: number;
  finished: boolean;
};

/** Where the protocol is at `elapsed` seconds. Pure: the caller owns the clock. */
export function stateAt(steps: Step[], elapsed: number): ProtocolState {
  const trialsTotal = steps.filter((s) => s.kind === 'trial').length;
  let acc = 0;
  for (let i = 0; i < steps.length; i++) {
    const step = steps[i];
    if (step.kind === 'done') break;
    if (elapsed < acc + step.duration) {
      return {
        step,
        stepIndex: i,
        remaining: acc + step.duration - elapsed,
        trialsDone: steps.slice(0, i).filter((s) => s.kind === 'trial').length,
        trialsTotal,
        finished: false,
      };
    }
    acc += step.duration;
  }
  return {
    step: steps[steps.length - 1],
    stepIndex: steps.length - 1,
    remaining: 0,
    trialsDone: trialsTotal,
    trialsTotal,
    finished: true,
  };
}
