import { useCallback, useEffect, useRef, useState } from 'react';
import {
  Condition,
  DEFAULT_OPTIONS,
  ProtocolState,
  Step,
  buildSequence,
  conditionLabel,
  conditionSpeech,
  sequenceDuration,
  stateAt,
} from './utils/protocol';
import {
  playConditionCue,
  playSessionEnd,
  playTrialEnd,
  playTrialStart,
  speak,
  stopSpeaking,
  unlockAudio,
} from './utils/cueAudio';

type Props = {
  /** Emits each phase change so it can be recorded alongside the signal. */
  onCue: (cue: {
    phase: Step['kind'];
    condition?: Condition;
    trial?: number;
  }) => void;
  disabled?: boolean;
};

const mmss = (seconds: number) => {
  const s = Math.max(0, Math.ceil(seconds));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
};

export default function CuedProtocol({ onCue, disabled }: Props) {
  const [steps, setSteps] = useState<Step[]>([]);
  const [state, setState] = useState<ProtocolState | null>(null);
  const [running, setRunning] = useState(false);
  const startedAt = useRef(0);
  const lastStepIndex = useRef(-1);
  // onCue changes identity on every render of the parent; a ref keeps the timer
  // effect from tearing down and restarting the protocol underneath itself.
  const onCueRef = useRef(onCue);
  useEffect(() => { onCueRef.current = onCue; }, [onCue]);

  const stop = useCallback((finished: boolean) => {
    setRunning(false);
    stopSpeaking();
    if (finished) playSessionEnd();
    onCueRef.current({ phase: 'done' });
  }, []);

  const start = useCallback(() => {
    // Must happen inside the click: browsers only allow audio from a gesture,
    // and a protocol nobody can hear is useless for the eyes-closed conditions.
    unlockAudio();
    const seq = buildSequence();
    setSteps(seq);
    lastStepIndex.current = -1;
    startedAt.current = performance.now();
    setRunning(true);
  }, []);

  useEffect(() => {
    if (!running || steps.length === 0) return;

    const tick = () => {
      const elapsed = (performance.now() - startedAt.current) / 1000;
      const next = stateAt(steps, elapsed);
      setState(next);

      if (next.stepIndex !== lastStepIndex.current) {
        lastStepIndex.current = next.stepIndex;
        const { step } = next;

        if (step.kind === 'instruct' && step.condition) {
          playConditionCue(step.condition);
          speak(conditionSpeech(step.condition));
        } else if (step.kind === 'trial') {
          playTrialStart();
        } else if (step.kind === 'rest') {
          playTrialEnd();
        }

        onCueRef.current({
          phase: step.kind,
          condition: step.condition,
          trial: step.trialIndex,
        });
      }

      if (next.finished) stop(true);
    };

    tick();
    const id = window.setInterval(tick, 100);
    return () => clearInterval(id);
  }, [running, steps, stop]);

  const total = steps.length ? sequenceDuration(steps) : 0;
  const step = state?.step;
  const condition = step?.condition;

  // Eyes-closed trials must not rely on anything visual, so the screen is a
  // convenience for the operator rather than the instruction itself.
  const bigText = !running
    ? null
    : step?.kind === 'trial' && condition
      ? conditionLabel(condition)
      : step?.kind === 'instruct' && condition
        ? `Next: ${conditionLabel(condition)}`
        : step?.kind === 'rest'
          ? 'Rest'
          : 'Finished';

  const accent =
    step?.kind === 'trial'
      ? condition?.task === 'focus' ? 'var(--success)' : 'var(--primary)'
      : 'var(--text-secondary)';

  return (
    <section className="glass-card metric-card">
      <h3 className="card-title">3. Cued Protocol</h3>

      {!running && (
        <div style={{ marginTop: '14px' }}>
          <p style={{ fontSize: '0.82rem', color: 'var(--text-secondary)', lineHeight: 1.5 }}>
            Four conditions — eyes open or closed, calm or focused — in a balanced random
            order. Every cue is spoken and beeped, so you never need to look at the screen.
          </p>
          <p style={{ fontSize: '0.75rem', color: 'var(--text-muted)', margin: '8px 0 0' }}>
            {DEFAULT_OPTIONS.repeats} repeats × 4 conditions · {DEFAULT_OPTIONS.trialSeconds}s
            trials · about {mmss(sequenceDuration(buildSequence()))} total
          </p>
          <p style={{ fontSize: '0.75rem', color: 'var(--warning)', margin: '10px 0 0' }}>
            Turn your volume up and start a recording first, or the cues will not be saved.
          </p>
          <button
            onClick={start}
            disabled={disabled}
            style={{ width: '100%', marginTop: '12px' }}
          >
            Start protocol
          </button>
        </div>
      )}

      {running && state && (
        <div style={{ marginTop: '16px' }}>
          <div className="text-center" style={{ marginBottom: '14px' }}>
            <div style={{ fontSize: '1.5rem', fontWeight: 800, color: accent, lineHeight: 1.2 }}>
              {bigText}
            </div>
            <div style={{ fontSize: '2.6rem', fontWeight: 800, marginTop: '6px' }}>
              {Math.ceil(state.remaining)}
            </div>
            <div style={{ fontSize: '0.75rem', color: 'var(--text-secondary)' }}>
              trial {Math.min(state.trialsDone + 1, state.trialsTotal)} of {state.trialsTotal}
              {' · '}
              {mmss(total - (performance.now() - startedAt.current) / 1000)} left
            </div>
          </div>

          <div style={{
            height: '6px', borderRadius: '999px',
            background: 'rgba(255,255,255,0.07)', overflow: 'hidden',
          }}>
            <div style={{
              height: '100%',
              width: `${(state.trialsDone / Math.max(1, state.trialsTotal)) * 100}%`,
              background: 'var(--primary)',
              transition: 'width 0.3s',
            }} />
          </div>

          <button
            onClick={() => stop(false)}
            style={{
              width: '100%', marginTop: '14px', fontSize: '0.85rem',
              background: 'rgba(239, 68, 68, 0.1)',
              borderColor: 'var(--danger)', color: 'var(--danger)',
            }}
          >
            Abandon protocol
          </button>
        </div>
      )}
    </section>
  );
}
