// Audio cues for the protocol.
//
// Audio is not a convenience here: half the conditions are eyes-closed, so a
// visual cue cannot reach the participant at all. It also means the sound has to
// carry the *instruction*, not just the timing.
//
// Two layers, deliberately redundant:
//   - synthesised tones, which always work: no network, no voice packs, no
//     permissions. A distinct pattern encodes each condition.
//   - spoken words over the top when the device can manage it, because
//     remembering four tone patterns is a burden nobody needs.
//
// Speech is best-effort. Android TTS voices are a download, and this rig runs
// off-grid, so the tones alone must be sufficient to run the whole session.

import { Condition } from './protocol';

let ctx: AudioContext | null = null;

/** Must be called from a user gesture — browsers refuse audio otherwise. */
export function unlockAudio(): boolean {
  try {
    if (!ctx) {
      const Ctor = window.AudioContext || (window as unknown as {
        webkitAudioContext: typeof AudioContext;
      }).webkitAudioContext;
      ctx = new Ctor();
    }
    if (ctx.state === 'suspended') void ctx.resume();
    return true;
  } catch {
    return false;
  }
}

export const audioReady = () => !!ctx && ctx.state === 'running';

function tone(freq: number, startAt: number, durationMs: number, gain = 0.22) {
  if (!ctx) return;
  const osc = ctx.createOscillator();
  const amp = ctx.createGain();
  osc.type = 'sine';
  osc.frequency.value = freq;

  const t0 = ctx.currentTime + startAt / 1000;
  const t1 = t0 + durationMs / 1000;
  // Ramp rather than switch: an abrupt gate produces a click that is unpleasant
  // through headphones and, worse, shows up in the EEG as a startle response.
  amp.gain.setValueAtTime(0.0001, t0);
  amp.gain.exponentialRampToValueAtTime(gain, t0 + 0.02);
  amp.gain.setValueAtTime(gain, t1 - 0.03);
  amp.gain.exponentialRampToValueAtTime(0.0001, t1);

  osc.connect(amp).connect(ctx.destination);
  osc.start(t0);
  osc.stop(t1 + 0.02);
}

/** Eyes are encoded by how many beeps, task by their pitch.
 *
 *   one beep  = eyes open      low  = calm
 *   two beeps = eyes closed    high = focus
 */
export function playConditionCue(condition: Condition) {
  if (!ctx) return;
  const pitch = condition.task === 'focus' ? 880 : 440;
  const beeps = condition.eyes === 'closed' ? 2 : 1;
  for (let i = 0; i < beeps; i++) tone(pitch, i * 260, 180);
}

/** Marks a trial actually starting, distinct from the instruction. */
export function playTrialStart() {
  tone(660, 0, 120, 0.18);
}

/** Marks a trial ending — falling pair, so it cannot be mistaken for a start. */
export function playTrialEnd() {
  tone(520, 0, 130, 0.16);
  tone(390, 150, 190, 0.16);
}

export function playSessionEnd() {
  [523, 659, 784].forEach((f, i) => tone(f, i * 170, 260, 0.2));
}

let speechChecked = false;
let speechUsable = false;

/** Speak best-effort. Never throws, never blocks the protocol. */
export function speak(text: string) {
  try {
    if (!('speechSynthesis' in window)) return;
    if (!speechChecked) {
      speechChecked = true;
      speechUsable = window.speechSynthesis.getVoices().length > 0;
      // Voices often load late; re-check once rather than giving up forever.
      window.speechSynthesis.addEventListener?.('voiceschanged', () => {
        speechUsable = window.speechSynthesis.getVoices().length > 0;
      });
    }
    if (!speechUsable) return;
    window.speechSynthesis.cancel();
    const utterance = new SpeechSynthesisUtterance(text);
    utterance.rate = 0.95;
    utterance.volume = 1;
    window.speechSynthesis.speak(utterance);
  } catch {
    // A device without usable TTS still gets the tones.
  }
}

export function stopSpeaking() {
  try {
    window.speechSynthesis?.cancel();
  } catch {
    /* nothing to stop */
  }
}
