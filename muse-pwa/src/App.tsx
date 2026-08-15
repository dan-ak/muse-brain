import { useState, useEffect, useRef, useCallback } from 'react';
import { connectMuse, Muse, MuseCircularBuffer } from 'web-muse';
import {
  SAMPLE_RATE,
  WINDOW_SIZE,
  CHANNELS,
  sanitizeAndInterpolate,
  calculatePowerSpectrum,
  powerByBand,
  mainsRatio,
  MAINS_RATIO_BAD,
  BandPowers
} from './utils/dsp';
import { fetchSeats, playerSocketUrl } from './serverApi';
import CuedProtocol from './CuedProtocol';
import { Condition } from './utils/protocol';
import './App.css';

// Channel labels for Muse 2
const CHANNEL_LABELS = ['TP9 (Left Ear)', 'AF7 (Left Forehead)', 'AF8 (Right Forehead)', 'TP10 (Right Ear)'];
const CHANNEL_COLORS = ['#3b82f6', '#10b981', '#f59e0b', '#ec4899']; // Blue, Green, Yellow, Pink

// Web Bluetooth only exists in a secure context. Served over plain HTTP from a
// LAN address, navigator.bluetooth is simply undefined — so check up front and
// explain it, rather than letting the pair button throw something cryptic.
const bluetoothAvailable = window.isSecureContext && 'bluetooth' in navigator;

// Close code the server uses when it hands a seat to a newer connection.
const SEAT_TAKEN_CODE = 1001;

// Raw capture. Every web-muse buffer holds 256 samples, which at 256 Hz is
// exactly one second of EEG headroom — and when full the buffer discards
// incoming samples rather than overwriting old ones. Draining on a timer rather
// than requestAnimationFrame matters: rAF stops entirely in a hidden tab, so a
// locked screen would lose raw samples outright instead of merely slowing down.
// 30 ms rather than 100 gives real headroom against the buffer: a hidden tab
// clamps timers to about 1 Hz, and 100 ms was already exactly the buffer's one
// second of EEG, so the "guarantees the buffers get emptied" claim did not hold
// the moment a screen locked.
const DRAIN_INTERVAL_MS = 30;
const RAW_SEND_HZ = 10;

// Ceiling on queued rows per stream. aiohttp refuses websocket messages over
// 4 MiB, so an unbounded queue during a disconnect turns into one oversized
// batch that kills the socket and loses everything. Two seconds of EEG is far
// more than a healthy send cycle needs.
const MAX_QUEUED_ROWS = 512;

type RawQueue = {
  eeg: number[][];
  ppg: number[][];
  acc: number[][];
  gyro: number[][];
  bands: number[][];
};

const emptyRawQueue = (): RawQueue => ({ eeg: [], ppg: [], acc: [], gyro: [], bands: [] });

/** Append rows, dropping the oldest past the cap and reporting what was lost.
 *
 * Bounding here rather than letting the queue grow is what keeps a disconnect
 * from becoming an oversized batch on reconnect. Dropping the oldest keeps the
 * most recent signal, and the loss is reported so it is never silent.
 */
const pushCapped = (queue: number[][], rows: number[][]): number => {
  queue.push(...rows);
  const excess = queue.length - MAX_QUEUED_ROWS;
  if (excess <= 0) return 0;
  queue.splice(0, excess);
  return excess;
};

function App() {
  // Device & Connection State
  const [museDevice, setMuseDevice] = useState<Muse | null>(null);
  const [isConnected, setIsConnected] = useState(false);
  const [isMock, setIsMock] = useState(false);
  const [battery, setBattery] = useState<number | null>(null);
  
  // WebSocket State. The roster is owned by the server, so adding a seat is a
  // server flag rather than a rebuild of this bundle.
  const [seats, setSeats] = useState<string[]>([]);
  const [playerId, setPlayerId] = useState<string>('p1');
  const [seatTaken, setSeatTaken] = useState(false);
  const [screenHeld, setScreenHeld] = useState(false);
  const wakeLockRef = useRef<WakeLockSentinel | null>(null);
  // Bumped to deliberately re-enter the seat after another device took it.
  const [reclaimNonce, setReclaimNonce] = useState(0);
  const [wsStatus, setWsStatus] = useState<'disconnected' | 'connecting' | 'connected'>('disconnected');
  
  // DSP & Live EEG Stats
  const [bandPowersState, setBandPowersState] = useState<BandPowers>({
    delta: 0, theta: 0, alpha: 0, beta: 0, gamma: 0
  });
  const [focusScore, setFocusScore] = useState<number>(0);
  const [normalizedDrive, setNormalizedDrive] = useState<number>(0); // Range [-1.0, 1.0]
  const [channelQualities, setChannelQualities] = useState<('good' | 'noise' | 'disconnected')[]>(
    ['disconnected', 'disconnected', 'disconnected', 'disconnected']
  );
  // Mains hum per channel, relative to the broadband floor. A separate axis from
  // contact quality because it has a different remedy: wet the pad, do not
  // re-seat the band.
  const [mainsRatios, setMainsRatios] = useState<number[]>([1, 1, 1, 1]);

  // Calibration State
  const [calState, setCalState] = useState<'idle' | 'relax' | 'focus' | 'done'>('idle');
  const [calTimeLeft, setCalTimeLeft] = useState(15);
  const [baseline, setBaseline] = useState(0.0);
  const [halfRange, setHalfRange] = useState(1.0);

  // Refs for background data streaming & WebSockets
  const wsRef = useRef<WebSocket | null>(null);
  const eegBuffersRef = useRef<number[][]>([[], [], [], []]); // last 512 samples per channel
  const scrollBuffersRef = useRef<number[][]>([[], [], [], []]); // last 200 samples for UI drawing
  const calibrationSamplesRef = useRef<number[]>([]);
  const relaxSamplesRef = useRef<number[]>([]);
  const focusSamplesRef = useRef<number[]>([]);
  const bandPowersRef = useRef<BandPowers>({
    delta: 0, theta: 0, alpha: 0, beta: 0, gamma: 0
  });
  // False until the DSP interval has produced a real reading. A phone whose
  // EEG buffer has not yet filled (or never fills) would otherwise stream
  // this all-zero placeholder forever, and zero reads as an exactly neutral
  // score — indistinguishable from a real reading at the calibrated
  // baseline. Sending null while this is false lets the Pi tell "no data
  // yet" apart from "calm at baseline" instead of fabricating the latter.
  const bandsValidRef = useRef(false);

  // Raw capture. Queues accumulate between sends; a ref rather than state
  // because the streaming effect rebuilds often and must not lose samples.
  const rawQueueRef = useRef<RawQueue>(emptyRawQueue());
  const overflowsRef = useRef<Record<string, number>>({});
  // Last drain time per stream, used to prove sample loss from elapsed time.
  const lastDrainRef = useRef<Record<string, number>>({});

  // The data loop reads these through refs rather than listing them as effect
  // dependencies. focusScore changes at 4 Hz, so depending on it tore down and
  // rebuilt the whole loop — including the drain interval that raw capture
  // relies on — four times a second, and reset the mock generator's phase with
  // it. The loop should outlive a changing reading.
  const focusScoreRef = useRef(0);
  const baselineRef = useRef(0);
  const halfRangeRef = useRef(1);
  const calStateRef = useRef<'idle' | 'relax' | 'focus' | 'done'>('idle');
  const playerIdRef = useRef('p1');
  // Read by the data loop, so a ref: reading state there would capture a stale
  // value from whichever render created the closure.
  const recordingRef = useRef(false);
  const [serverRecording, setServerRecording] = useState(false);

  // Canvas Reference
  const canvasRef = useRef<HTMLCanvasElement | null>(null);

  // Keep the data loop's refs in step with the state the UI renders from.
  useEffect(() => { focusScoreRef.current = focusScore; }, [focusScore]);
  useEffect(() => { baselineRef.current = baseline; }, [baseline]);
  useEffect(() => { halfRangeRef.current = halfRange; }, [halfRange]);
  useEffect(() => { calStateRef.current = calState; }, [calState]);
  useEffect(() => { playerIdRef.current = playerId; }, [playerId]);

  // 0. Learn which seats this server offers.
  useEffect(() => {
    let cancelled = false;
    fetchSeats()
      .then((available) => {
        if (cancelled || available.length === 0) return;
        setSeats(available);
        // Only correct the selection if the current one is not on offer,
        // so a deliberate choice survives a refetch.
        setPlayerId((current) => (available.includes(current) ? current : available[0]));
      })
      .catch((err) => console.error('Could not fetch the seat roster:', err));
    return () => {
      cancelled = true;
    };
  }, []);

  // 0b. Hold the screen awake while a headset is streaming.
  //
  // Browsers clamp timers in hidden tabs to about 1 Hz, so the moment a phone
  // locks or the tab goes to the background this client drops from 30 Hz to
  // 1 Hz. Nothing errors and the seat still reads connected, so the loss is
  // invisible until you look at the recording afterwards.
  useEffect(() => {
    if (!isConnected || !('wakeLock' in navigator)) return;

    let cancelled = false;

    const acquire = async () => {
      try {
        const lock = await navigator.wakeLock.request('screen');
        if (cancelled) {
          void lock.release();
          return;
        }
        wakeLockRef.current = lock;
        setScreenHeld(true);
        // The browser drops the lock on its own when the page hides, so track
        // that rather than assuming we still hold it.
        lock.addEventListener('release', () => setScreenHeld(false));
      } catch (err) {
        // Refusal is not fatal — low battery mode declines these. Stream on.
        console.warn('Screen wake lock refused:', err);
        setScreenHeld(false);
      }
    };

    // A lock cannot be re-acquired while hidden, so retake it on return
    // instead of requesting once and assuming it survives.
    const onVisibility = () => {
      if (document.visibilityState === 'visible') void acquire();
    };

    void acquire();
    document.addEventListener('visibilitychange', onVisibility);

    return () => {
      cancelled = true;
      document.removeEventListener('visibilitychange', onVisibility);
      void wakeLockRef.current?.release().catch(() => {});
      wakeLockRef.current = null;
      setScreenHeld(false);
    };
  }, [isConnected]);

  // 1. Maintain WebSocket Connection
  useEffect(() => {
    let reconnectTimeout: number;
    let cancelled = false;

    // Samples captured under the previous seat must never be shipped on this
    // one: the server takes the seat from the socket's URL, not the payload, so
    // a queue surviving a seat change writes one person's EEG under another's
    // name — unrecoverable once on disk.
    recordingRef.current = false;
    rawQueueRef.current = emptyRawQueue();
    overflowsRef.current = {};
    lastDrainRef.current = {};

    const connectWS = () => {
      if (cancelled) return;

      // Detach the previous socket's handlers before closing it. Otherwise its
      // onclose fires during the swap and schedules a second reconnect chain,
      // and the chains multiply.
      const previous = wsRef.current;
      if (previous) {
        previous.onopen = null;
        previous.onclose = null;
        previous.onerror = null;
        previous.close();
      }

      setWsStatus('connecting');
      const wsUrl = playerSocketUrl(playerId);
      console.log(`Connecting to WebSocket: ${wsUrl}`);

      const socket = new WebSocket(wsUrl);
      wsRef.current = socket;

      socket.onopen = () => {
        if (cancelled) return;
        setSeatTaken(false);
        setWsStatus('connected');
        console.log('WebSocket connection established.');
      };

      // The server announces session state on connect and whenever it changes,
      // so raw capture only runs while something is actually recording.
      socket.onmessage = (event) => {
        if (cancelled) return;
        try {
          const msg = JSON.parse(event.data);
          if (msg?.type === 'recording') {
            recordingRef.current = !!msg.active;
            setServerRecording(!!msg.active);
          }
        } catch {
          // A malformed frame from the server is not worth reacting to.
        }
      };

      socket.onclose = (event) => {
        if (cancelled) return;
        setWsStatus('disconnected');

        // Stop capturing the moment the link drops. The queue is bounded, but
        // without this a disconnect mid-session keeps filling it and the first
        // batch after reconnect is both oversized and misattributed in time.
        // The server re-announces session state on connect, so this is restored
        // automatically rather than needing to be remembered.
        recordingRef.current = false;
        setServerRecording(false);
        rawQueueRef.current = emptyRawQueue();
        overflowsRef.current = {};
        lastDrainRef.current = {};

        // The server always hands a seat to the newest claimant, so that a
        // phone waking from sleep can reclaim it. Reconnecting here would take
        // the seat straight back, and two live devices on one seat would
        // displace each other every few seconds forever. Stop and say so.
        if (event.code === SEAT_TAKEN_CODE && event.reason.includes('replaced')) {
          console.warn('Seat claimed by another device; not reconnecting.');
          setSeatTaken(true);
          return;
        }

        console.log('WebSocket connection lost. Reconnecting in 3s...');
        reconnectTimeout = window.setTimeout(connectWS, 3000);
      };

      socket.onerror = (err) => {
        console.error('WebSocket Error:', err);
      };
    };

    connectWS();

    return () => {
      cancelled = true;
      clearTimeout(reconnectTimeout);
      const socket = wsRef.current;
      if (socket) {
        socket.onclose = null;
        socket.close();
      }
    };
  }, [playerId, reclaimNonce]);

  // 2. Headset Data Loop (draining the MuseCircularBuffer)
  useEffect(() => {
    if (!isConnected || !museDevice) {
      setChannelQualities(['disconnected', 'disconnected', 'disconnected', 'disconnected']);
      return;
    }

    let mockDataGeneratorInterval: number;
    let animationFrameId: number;

    const noteLoss = (stream: string, samples: number) => {
      if (samples <= 0) return;
      overflowsRef.current[stream] = (overflowsRef.current[stream] ?? 0) + samples;
    };

    /** Estimate samples lost since this stream was last drained.
     *
     * `isFull` is a false positive: the buffer sets it on the write that fills
     * the last slot, before anything has been discarded, so a poll landing in
     * that window reported loss for a complete recording. Elapsed time is
     * provable instead — a gap longer than the buffer holds means the surplus
     * was certainly discarded, and it yields a sample count rather than a count
     * of times a flag happened to be set.
     */
    const estimateLoss = (stream: string, rate: number, capacity: number) => {
      const now = performance.now();
      const previous = lastDrainRef.current[stream];
      lastDrainRef.current[stream] = now;
      if (previous === undefined) return 0;
      const gap = (now - previous) / 1000;
      return Math.max(0, Math.round(gap * rate) - capacity);
    };

    /** Read aligned rows across parallel per-channel buffers.
     *
     * Reads the same count from every channel and leaves any excess in place
     * rather than levelling with Math.min. Channels arrive in separate BLE
     * notifications, so an overflow discards an unequal number from each, and
     * dropping the surplus would permanently pair TP9[i] with AF7[i+k] — a file
     * that looks well-formed while every cross-channel result is wrong.
     */
    const drainAligned = (
      buffers: MuseCircularBuffer[] | undefined,
      width: number,
      stream: string,
      rate: number,
    ) => {
      const rows: number[][] = [];
      if (!buffers) return rows;
      const bufs = buffers.slice(0, width);
      if (bufs.length < width || bufs.some((b) => !b)) return rows;

      const shortest = Math.min(...bufs.map((b) => b.length));
      noteLoss(stream, estimateLoss(stream, rate, bufs[0].memory.length));

      // Uneven fill across channels is NOT reported as loss. Channels arrive in
      // separate BLE notifications, so at any instant the buffers differ by a
      // few samples; the surplus stays put and drains on the next poll. A real
      // session showed 99.9% capture while every batch reported a gap, which
      // made the gap log useless — and worse, would have masked a real dropout.
      // Genuine loss is caught by the elapsed-time test above, which is
      // provable rather than inferred from a transient.
      for (let i = 0; i < shortest; i++) {
        const row: number[] = [];
        for (const b of bufs) {
          const sample = b.read();
          if (sample === null) return rows;
          row.push(sample);
        }
        rows.push(row);
      }
      return rows;
    };

    // A. Read samples from the Muse buffers into our sliding windows, and into
    // the raw queue when a session is recording.
    const pollBuffers = () => {
      const eegBufs = [];
      for (let ch = 0; ch < CHANNELS; ch++) {
        if (museDevice.eeg[ch]) eegBufs.push(museDevice.eeg[ch]);
      }

      if (eegBufs.length === CHANNELS) {
        const available = Math.min(...eegBufs.map((b) => b.length));
        noteLoss('eeg', estimateLoss('eeg', SAMPLE_RATE, eegBufs[0].memory.length));
        // Transient channel skew is normal and self-correcting — see drainAligned.
        const captured: number[][] = [];
        for (let i = 0; i < available; i++) {
          const row: number[] = [];
          for (let ch = 0; ch < CHANNELS; ch++) {
            const sample = eegBufs[ch].read();
            if (sample === null) break;
            row.push(sample);

            // FFT sliding window (512 samples)
            eegBuffersRef.current[ch].push(sample);
            if (eegBuffersRef.current[ch].length > WINDOW_SIZE) {
              eegBuffersRef.current[ch].shift();
            }

            // UI scrolling waveform (200 samples)
            scrollBuffersRef.current[ch].push(sample);
            if (scrollBuffersRef.current[ch].length > 200) {
              scrollBuffersRef.current[ch].shift();
            }
          }
          // Rows go to the recorder aligned across channels, so drain them here
          // rather than per channel — the FFT does not care about alignment but
          // a recording of raw EEG very much does.
          if (row.length === CHANNELS) captured.push(row);
        }
        if (recordingRef.current) {
          noteLoss('eeg', pushCapped(rawQueueRef.current.eeg, captured));
        }
      }

      // These are drained whether or not we are recording. Left alone they stay
      // permanently full, which both wastes the sensor and makes the elapsed
      // time since the last drain meaningless.
      const ppg = drainAligned(museDevice.ppg, 3, 'ppg', 64);
      const acc = drainAligned(museDevice.accelerometer, 3, 'acc', 52);
      const gyro = drainAligned(museDevice.gyroscope, 3, 'gyro', 52);
      if (recordingRef.current) {
        noteLoss('ppg', pushCapped(rawQueueRef.current.ppg, ppg));
        noteLoss('acc', pushCapped(rawQueueRef.current.acc, acc));
        noteLoss('gyro', pushCapped(rawQueueRef.current.gyro, gyro));
      }

      // Read battery level if available
      if (museDevice.batteryLevel !== null) {
        setBattery(museDevice.batteryLevel);
      }
    };

    const pollOnFrame = () => {
      pollBuffers();
      animationFrameId = requestAnimationFrame(pollOnFrame);
    };

    // B. If in mock mode, generate synthetic brain wave data at 256Hz
    if (isMock) {
      let t = 0;
      mockDataGeneratorInterval = window.setInterval(() => {
        t += 1 / SAMPLE_RATE;
        for (let ch = 0; ch < CHANNELS; ch++) {
          // Create synthetic signals: theta (6Hz), alpha (10Hz), beta (20Hz)
          // Add some random noise
          const thetaWave = 15 * Math.sin(2 * Math.PI * 6.0 * t + ch);
          const alphaWave = (10 + 20 * Math.sin(t * 0.1)) * Math.sin(2 * Math.PI * 10.0 * t + ch); // alpha swells
          const betaWave = (5 + 10 * Math.cos(t * 0.2)) * Math.sin(2 * Math.PI * 20.0 * t + ch); // beta surges
          const noise = (Math.random() - 0.5) * 8;
          const totalSignal = thetaWave + alphaWave + betaWave + noise;

          // Write simulated raw value to muse circular buffer
          museDevice.eeg[ch].write(totalSignal);
        }
      }, 1000 / SAMPLE_RATE);
    }

    // Start draining buffers. The animation frame keeps the waveform smooth
    // while visible; the interval is what guarantees the buffers get emptied
    // even when the tab is hidden and rAF has stopped firing.
    pollOnFrame();
    const drainInterval = window.setInterval(pollBuffers, DRAIN_INTERVAL_MS);

    // C. DSP Calculations: FFT + Band Powers (Runs at 4Hz / every 250ms)
    const dspInterval = window.setInterval(() => {
      // 1. Calculate Signal Quality (standard deviation over the last 128 samples)
      const qualities = eegBuffersRef.current.map((rawData) => {
        if (rawData.length < 128) return 'disconnected';
        
        // Take a window of 128 samples (0.5s) to assess stability
        const samples = rawData.slice(-128);
        const mean = samples.reduce((a, b) => a + b, 0) / samples.length;
        const variance = samples.reduce((a, b) => a + Math.pow(b - mean, 2), 0) / samples.length;
        const stdDev = Math.sqrt(variance);
        
        // If simulated, it's always good
        if (isMock) return 'good';
        
        if (stdDev < 1.5) return 'disconnected'; // flatline / off head
        if (stdDev > 150.0) return 'noise'; // too noisy / poor fit
        return 'good';
      });
      setChannelQualities(qualities);

      // Check if we have gathered enough samples for FFT
      if (eegBuffersRef.current[0].length < WINDOW_SIZE) return;

      const channelPowers: BandPowers[] = [];
      const nextMains = [1, 1, 1, 1];

      for (let ch = 0; ch < CHANNELS; ch++) {
        const rawData = eegBuffersRef.current[ch];
        const sanitized = sanitizeAndInterpolate(rawData);
        const spectrum = calculatePowerSpectrum(sanitized);
        const powers = powerByBand(spectrum);
        channelPowers.push(powers);
        // Free to compute here: the spectrum already exists for the band powers.
        nextMains[ch] = mainsRatio(spectrum);

        // Per-channel band powers are computed here and were previously thrown
        // away, keeping only their average. They are the most useful derived
        // signal after the raw trace, and free to record.
        if (recordingRef.current) {
          rawQueueRef.current.bands.push([
            ch, powers.delta, powers.theta, powers.alpha, powers.beta, powers.gamma,
          ]);
        }
      }

      setMainsRatios(nextMains);

      // Average band powers across all 4 channels
      const avgPowers: BandPowers = {
        delta: 0, theta: 0, alpha: 0, beta: 0, gamma: 0
      };
      
      channelPowers.forEach(p => {
        avgPowers.delta += p.delta;
        avgPowers.theta += p.theta;
        avgPowers.alpha += p.alpha;
        avgPowers.beta += p.beta;
        avgPowers.gamma += p.gamma;
      });

      avgPowers.delta /= CHANNELS;
      avgPowers.theta /= CHANNELS;
      avgPowers.alpha /= CHANNELS;
      avgPowers.beta /= CHANNELS;
      avgPowers.gamma /= CHANNELS;

      setBandPowersState(avgPowers);
      bandPowersRef.current = avgPowers;
      bandsValidRef.current = true;

      // Focus Score = Log10(Beta) - Log10(Theta)
      // High beta/theta ratio = high attention, alertness, or focus.
      const betaLog = Math.log10(avgPowers.beta + 1e-6);
      const thetaLog = Math.log10(avgPowers.theta + 1e-6);
      const rawFocus = betaLog - thetaLog;
      setFocusScore(rawFocus);

      // Collect samples if calibrating
      if (calibrationSamplesRef.current !== null) {
        calibrationSamplesRef.current.push(rawFocus);
      }
    }, 250);

    // D. WebSocket Streaming: Send updates to Pi at 30Hz
    const wsStreamInterval = window.setInterval(() => {
      if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
        const score = focusScoreRef.current;
        const phase = calStateRef.current;

        // Calculate dynamic drive normalized score based on current focus
        let drive = (score - baselineRef.current) / halfRangeRef.current;
        drive = Math.max(-1.0, Math.min(1.0, drive));

        // Push state up
        setNormalizedDrive(drive);

        wsRef.current.send(JSON.stringify({
          playerId: playerIdRef.current,
          rawScore: score,
          normalizedScore: drive,
          calibrationPhase: phase,
          isCalibrating: phase === 'relax' || phase === 'focus',
          // null until the DSP has produced a real reading — see bandsValidRef.
          // All-zero bands would work out to exactly the calibrated baseline,
          // reading as a real, perfectly neutral score rather than an absent one.
          bands: bandsValidRef.current ? bandPowersRef.current : null
        }));
      }
    }, 1000 / 30);

    // E. Raw capture: ship accumulated samples in batches while recording.
    const rawSendInterval = window.setInterval(() => {
      const socket = wsRef.current;
      if (!socket || socket.readyState !== WebSocket.OPEN) return;

      const queue = rawQueueRef.current;
      const overflows = overflowsRef.current;
      const hasSamples =
        queue.eeg.length || queue.ppg.length || queue.acc.length ||
        queue.gyro.length || queue.bands.length;

      if (!recordingRef.current) {
        // Not recording: discard rather than accumulate, or the first session
        // would open with a flood of samples predating it.
        if (hasSamples) rawQueueRef.current = emptyRawQueue();
        overflowsRef.current = {};
        return;
      }
      if (!hasSamples && !Object.keys(overflows).length) return;

      // Swap before sending so samples arriving mid-send are not lost.
      rawQueueRef.current = emptyRawQueue();
      overflowsRef.current = {};

      socket.send(JSON.stringify({
        type: 'raw',
        eeg: queue.eeg,
        ppg: queue.ppg,
        acc: queue.acc,
        gyro: queue.gyro,
        bands: queue.bands,
        overflows,
      }));
    }, 1000 / RAW_SEND_HZ);

    return () => {
      cancelAnimationFrame(animationFrameId);
      clearInterval(dspInterval);
      clearInterval(wsStreamInterval);
      clearInterval(mockDataGeneratorInterval);
      clearInterval(drainInterval);
      clearInterval(rawSendInterval);
      // The DSP is gone, so the bands it produced are history. Left true, a
      // reconnect would stream the *previous* session's band powers as if
      // fresh for the two seconds the FFT window takes to refill — the exact
      // "no data yet" case this flag exists to make visible.
      bandsValidRef.current = false;
    };
    // Deliberately excludes focusScore, baseline, halfRange, calState and
    // playerId: they are read through refs so a changing reading cannot tear
    // down the drain and send intervals underneath raw capture.
  }, [isConnected, museDevice, isMock]);

  // 3. Oscilloscope Waveform Renderer (Canvas)
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;

    let animFrame: number;

    const draw = () => {
      const w = canvas.width;
      const h = canvas.height;
      
      // Clear canvas
      ctx.fillStyle = '#060913';
      ctx.fillRect(0, 0, w, h);

      // Draw horizontal channel grids
      const chHeight = h / CHANNELS;
      ctx.strokeStyle = 'rgba(255,255,255,0.04)';
      ctx.lineWidth = 1;
      for (let i = 1; i < CHANNELS; i++) {
        ctx.beginPath();
        ctx.moveTo(0, i * chHeight);
        ctx.lineTo(w, i * chHeight);
        ctx.stroke();
      }

      // Draw waveforms
      if (isConnected) {
        for (let ch = 0; ch < CHANNELS; ch++) {
          const samples = scrollBuffersRef.current[ch];
          if (!samples || samples.length === 0) continue;

          ctx.strokeStyle = CHANNEL_COLORS[ch];
          ctx.lineWidth = 1.5;
          ctx.beginPath();

          const step = w / 200; // max length in buffer
          const yCenter = (ch * chHeight) + (chHeight / 2);

          for (let i = 0; i < samples.length; i++) {
            const x = i * step;
            // Scale raw EEG value (typically -100 to 100 µV) to pixel height
            const value = samples[i];
            const yOffset = (value / 150) * (chHeight / 2); // Scale divisor adjusts amplitude height
            const y = yCenter - Math.max(-chHeight/2, Math.min(chHeight/2, yOffset));

            if (i === 0) {
              ctx.moveTo(x, y);
            } else {
              ctx.lineTo(x, y);
            }
          }
          ctx.stroke();

          // Channel Label overlay
          ctx.fillStyle = 'rgba(255, 255, 255, 0.4)';
          ctx.font = '9px monospace';
          ctx.fillText(CHANNEL_LABELS[ch], 10, (ch * chHeight) + 14);
        }
      } else {
        // Draw centered idle text
        ctx.fillStyle = 'rgba(255, 255, 255, 0.15)';
        ctx.font = '14px sans-serif';
        ctx.textAlign = 'center';
        ctx.fillText('Waiting for Headset connection...', w / 2, h / 2);
      }

      animFrame = requestAnimationFrame(draw);
    };

    draw();

    return () => {
      cancelAnimationFrame(animFrame);
    };
  }, [isConnected]);

  // Cues go straight out rather than through the raw queue: they are tiny,
  // rare, and their timing is the whole point — batching would blur the very
  // boundary the recording exists to mark.
  const handleCue = useCallback(
    (cue: { phase: string; condition?: Condition; trial?: number }) => {
      const socket = wsRef.current;
      if (!socket || socket.readyState !== WebSocket.OPEN) return;
      socket.send(JSON.stringify({
        type: 'cue',
        phase: cue.phase,
        eyes: cue.condition?.eyes,
        task: cue.condition?.task,
        trial: cue.trial,
      }));
    },
    [],
  );

  // 4. Connection Handlers
  const handleConnectReal = async () => {
    if (!bluetoothAvailable) return;
    try {
      setIsMock(false);
      // Instantiate and connect real web bluetooth device
      const device = await connectMuse({ mock: false });
      setMuseDevice(device);
      setIsConnected(true);
    } catch (err: any) {
      console.error(err);
      alert('Bluetooth Connection Failed: ' + err.message);
    }
  };

  const handleConnectMock = async () => {
    try {
      setIsMock(true);
      // Mock mode instantiates local circular buffers
      const device = await connectMuse({ mock: true });
      setMuseDevice(device);
      setIsConnected(true);
    } catch (err: any) {
      console.error(err);
    }
  };

  const handleDisconnect = () => {
    if (museDevice) {
      museDevice.disconnect();
    }
    setMuseDevice(null);
    setIsConnected(false);
    setIsMock(false);
    setBattery(null);
    setChannelQualities(['disconnected', 'disconnected', 'disconnected', 'disconnected']);
    
    // Clear local buffers
    eegBuffersRef.current = [[], [], [], []];
    scrollBuffersRef.current = [[], [], [], []];
  };

  // 5. Calibration Wizard Sequence
  const runCalibration = async () => {
    // Stage 1: Relax Phase
    setCalState('relax');
    calibrationSamplesRef.current = [];
    let count = 15;
    setCalTimeLeft(count);
    
    const relaxInterval = setInterval(() => {
      count -= 1;
      setCalTimeLeft(count);
      if (count <= 0) {
        clearInterval(relaxInterval);
        // Save relax scores
        relaxSamplesRef.current = [...calibrationSamplesRef.current];
        
        // Stage 2: Focus Phase
        setCalState('focus');
        calibrationSamplesRef.current = [];
        count = 15;
        setCalTimeLeft(count);

        const focusInterval = setInterval(() => {
          count -= 1;
          setCalTimeLeft(count);
          if (count <= 0) {
            clearInterval(focusInterval);
            // Save focus scores
            focusSamplesRef.current = [...calibrationSamplesRef.current];
            
            // Finish Calibration
            finalizeCalibration();
          }
        }, 1000);
      }
    }, 1000);
  };

  const finalizeCalibration = () => {
    const relax = relaxSamplesRef.current;
    const focus = focusSamplesRef.current;

    if (relax.length > 0 && focus.length > 0) {
      const relaxMean = relax.reduce((a, b) => a + b, 0) / relax.length;
      const focusMean = focus.reduce((a, b) => a + b, 0) / focus.length;

      const base = 0.5 * (relaxMean + focusMean);
      let range = Math.abs(0.5 * (focusMean - relaxMean));
      if (range < 0.05) range = 1.0; // Avoid division by zero

      setBaseline(base);
      setHalfRange(range);
      setCalState('done');
      
      console.log(`Calibration Completed! Baseline: ${base.toFixed(4)}, HalfRange: ${range.toFixed(4)}`);

      // Notify Pi of completed calibration
      if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
        wsRef.current.send(JSON.stringify({
          playerId: playerId,
          event: 'calibration_complete',
          baseline: base,
          halfRange: range
        }));
      }
    } else {
      setCalState('idle');
    }
  };

  // Math helper to get relative band values for display
  const totalBandPower = Object.values(bandPowersState).reduce((a, b) => a + b, 0) || 1.0;

  // SVG Dial Math
  const radius = 70;
  const circumference = 2 * Math.PI * radius;
  // Map normalizedScore [-1.0, 1.0] -> percent [0.0, 100.0]
  const scorePercent = ((normalizedDrive + 1) / 2) * 100;
  const strokeDashoffset = circumference - (scorePercent / 100) * circumference;

  // Helper color map for electrode contact state
  const getQualityColor = (status: 'good' | 'noise' | 'disconnected') => {
    if (status === 'good') return '#10b981'; // Green
    if (status === 'noise') return '#f59e0b'; // Orange
    return '#ef4444'; // Red
  };

  // Mains hum is scored per electrode and shown alongside contact state. A
  // channel can read "good" on amplitude while being almost entirely hum, which
  // is exactly what happened on the ear clips in the first real recording — so
  // an electrode swamped by mains is drawn as a problem even when its
  // amplitude looks healthy.
  const hasMains = (ch: number) => mainsRatios[ch] >= MAINS_RATIO_BAD;
  const noisyElectrodes = [0, 1, 2, 3].filter(
    (ch) => hasMains(ch) && channelQualities[ch] !== 'disconnected'
  );

  const getElectrodeColor = (ch: number) => {
    if (channelQualities[ch] === 'disconnected') return '#ef4444';
    if (hasMains(ch)) return '#f59e0b';
    return getQualityColor(channelQualities[ch]);
  };

  const describeChannel = (ch: number) => {
    if (channelQualities[ch] === 'disconnected') return 'no contact';
    if (hasMains(ch)) return `mains hum ${Math.round(mainsRatios[ch])}x`;
    if (channelQualities[ch] === 'noise') return 'noisy';
    return 'good';
  };


  return (
    <div className="app-container">
      {/* Header Panel */}
      <header className="flex justify-between items-center glass-card" style={{ padding: '16px 24px', marginBottom: '24px' }}>
        <div className="flex items-center gap-4">
          <div style={{
            background: 'linear-gradient(135deg, #3b82f6 0%, #10b981 100%)',
            width: '40px',
            height: '40px',
            borderRadius: '10px',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            fontWeight: 'bold',
            fontSize: '1.2rem',
            color: 'white'
          }}>
            Ω
          </div>
          <div>
            <h2 style={{ margin: 0, fontSize: '1.2rem' }}>Meditation Battle Client</h2>
            <span style={{ fontSize: '0.8rem', color: 'var(--text-secondary)' }}>Powered by web-muse & Web Bluetooth</span>
          </div>
        </div>

        <div className="flex items-center gap-4">
          {serverRecording && (
            <div
              className="flex items-center"
              style={{
                fontSize: '0.8rem',
                fontWeight: 700,
                color: 'var(--danger)',
                background: 'rgba(239, 68, 68, 0.1)',
                border: '1px solid var(--danger)',
                padding: '5px 12px',
                borderRadius: '20px',
                gap: '7px',
              }}
              title="A session is recording. Your raw EEG is being saved."
            >
              <span className="rec-pip" />
              RECORDING
            </div>
          )}
          <div className="flex items-center" style={{ fontSize: '0.9rem' }}>
            <span className={`status-dot ${wsStatus === 'connected' ? 'active' : wsStatus === 'connecting' ? 'warning' : 'inactive'}`} />
            <span style={{ textTransform: 'capitalize' }}>Pi Socket: {wsStatus}</span>
          </div>
          {isConnected && (
            <div className="flex items-center" style={{ fontSize: '0.9rem', background: 'rgba(255,255,255,0.05)', padding: '6px 12px', borderRadius: '20px' }}>
              🔋 Battery: {battery !== null ? `${battery}%` : 'Reading...'}
            </div>
          )}
        </div>
      </header>

      {/* Main Grid */}
      <div className="dashboard-grid">
        
        {/* Left Hand Column: Settings & Calibration */}
        <div className="flex flex-col gap-4">
          
          {/* Connection Manager */}
          <section className="glass-card metric-card">
            <h3 className="card-title">1. Device & Seat</h3>

            <div className="flex flex-col gap-4" style={{ marginTop: '16px' }}>
              {!bluetoothAvailable && (
                <div style={{
                  background: 'rgba(239, 68, 68, 0.08)',
                  border: '1px solid var(--danger)',
                  borderRadius: '8px',
                  padding: '12px',
                  fontSize: '0.8rem',
                  lineHeight: 1.5
                }}>
                  <strong style={{ color: 'var(--danger)', display: 'block', marginBottom: '4px' }}>
                    Web Bluetooth unavailable
                  </strong>
                  {!window.isSecureContext ? (
                    <>
                      This page was loaded over an insecure connection
                      (<code>{window.location.protocol}//{window.location.host}</code>).
                      Browsers only expose Bluetooth on HTTPS or localhost. Open the
                      site over <code>https://</code> to pair a headset.
                    </>
                  ) : (
                    <>
                      This browser has no Web Bluetooth support. On Android use Chrome;
                      iOS Safari cannot pair a Muse at all.
                    </>
                  )}
                </div>
              )}

              {seatTaken && (
                <div style={{
                  background: 'rgba(245, 158, 11, 0.08)',
                  border: '1px solid var(--warning)',
                  borderRadius: '8px',
                  padding: '12px',
                  fontSize: '0.8rem',
                  lineHeight: 1.5
                }}>
                  <strong style={{ color: 'var(--warning)', display: 'block', marginBottom: '4px' }}>
                    Seat {playerId.toUpperCase()} taken by another device
                  </strong>
                  Another browser claimed this seat, so this one stopped streaming
                  rather than fighting over it. Pick a different seat, or take it back.
                  <button
                    onClick={() => setReclaimNonce((n) => n + 1)}
                    style={{ width: '100%', marginTop: '10px', fontSize: '0.8rem', padding: '8px' }}
                  >
                    Take seat {playerId.toUpperCase()} back
                  </button>
                </div>
              )}

              <div className="flex flex-col">
                <label style={{ fontSize: '0.75rem', color: 'var(--text-secondary)', marginBottom: '4px' }}>Player Seat</label>
                <select
                  value={playerId}
                  onChange={(e) => setPlayerId(e.target.value)}
                  disabled={seats.length === 0}
                >
                  {(seats.length > 0 ? seats : [playerId]).map((seat) => (
                    <option key={seat} value={seat}>
                      {seat.toUpperCase()}
                    </option>
                  ))}
                </select>
                <span style={{ fontSize: '0.7rem', color: 'var(--text-secondary)', marginTop: '6px' }}>
                  Streaming to {playerSocketUrl(playerId)}
                </span>
              </div>

              {!isConnected ? (
                <div className="flex flex-col gap-2" style={{ marginTop: '8px' }}>
                  <button
                    onClick={handleConnectReal}
                    disabled={!bluetoothAvailable}
                    className="glow-blue"
                    style={{
                      background: 'var(--primary)',
                      borderColor: 'rgba(59, 130, 246, 0.4)',
                      opacity: bluetoothAvailable ? 1 : 0.4,
                      cursor: bluetoothAvailable ? 'pointer' : 'not-allowed'
                    }}
                  >
                    Pair Muse 2 Headset
                  </button>
                  <button onClick={handleConnectMock} style={{ background: 'rgba(255, 255, 255, 0.05)', fontSize: '0.85rem' }}>
                    Run in Mock mode
                  </button>
                </div>
              ) : (
                <div className="flex flex-col gap-2" style={{ marginTop: '8px' }}>
                  <button onClick={handleDisconnect} style={{ background: 'rgba(239, 68, 68, 0.1)', borderColor: 'var(--danger)', color: 'var(--danger)' }}>
                    Disconnect {isMock ? 'Mock' : 'Muse'}
                  </button>
                  <span style={{ fontSize: '0.7rem', color: screenHeld ? 'var(--success)' : 'var(--warning)', lineHeight: 1.4 }}>
                    {screenHeld
                      ? '🔆 Screen kept awake — streaming at full rate'
                      : '⚠️ Screen not held. If it locks, streaming drops to ~1 Hz.'}
                  </span>
                </div>
              )}
            </div>
          </section>

          {/* Headset Fit Panel */}
          <section className="glass-card metric-card">
            <h3 className="card-title">Headset Fit Status</h3>
            <div className="flex justify-between items-center gap-4" style={{ marginTop: '16px' }}>
              <div style={{ flexGrow: 1, display: 'flex', flexDirection: 'column', gap: '8px', fontSize: '0.8rem', color: 'var(--text-secondary)' }}>
                {CHANNEL_LABELS.map((label, ch) => (
                  <div className="flex items-center gap-2" key={label}>
                    <span style={{
                      width: '8px', height: '8px', borderRadius: '50%', flexShrink: 0,
                      background: getElectrodeColor(ch),
                    }} />
                    <span>
                      {label}:{' '}
                      <strong style={{ color: getElectrodeColor(ch) }}>{describeChannel(ch)}</strong>
                    </span>
                  </div>
                ))}
              </div>

              {/* Head SVG map */}
              <div style={{ width: '100px', height: '100px', flexShrink: 0, position: 'relative' }}>
                <svg width="100" height="100" viewBox="0 0 100 100">
                  {/* Outer circle for Head shape */}
                  <circle cx="50" cy="50" r="35" fill="rgba(255,255,255,0.03)" stroke="rgba(255,255,255,0.15)" strokeWidth="2" />
                  {/* Nose */}
                  <path d="M50,15 L47,8 L53,8 Z" fill="rgba(255,255,255,0.1)" stroke="rgba(255,255,255,0.15)" strokeWidth="1.5" />
                  {/* Ears */}
                  <path d="M12,45 C10,45 10,55 12,55" fill="none" stroke="rgba(255,255,255,0.15)" strokeWidth="2" />
                  <path d="M88,45 C90,45 90,55 88,55" fill="none" stroke="rgba(255,255,255,0.15)" strokeWidth="2" />
                  
                  {/* Forehead Electrodes (AF7 & AF8) */}
                  <circle cx="38" cy="22" r="5" fill={getElectrodeColor(1)} style={{ transition: 'fill 0.3s' }} />
                  <circle cx="62" cy="22" r="5" fill={getElectrodeColor(2)} style={{ transition: 'fill 0.3s' }} />

                  {/* Ear Electrodes (TP9 & TP10) */}
                  <circle cx="20" cy="50" r="5" fill={getElectrodeColor(0)} style={{ transition: 'fill 0.3s' }} />
                  <circle cx="80" cy="50" r="5" fill={getElectrodeColor(3)} style={{ transition: 'fill 0.3s' }} />

                  {/* A ring marks hum specifically, so it reads differently from
                      a merely noisy contact at a glance. */}
                  {hasMains(1) && <circle cx="38" cy="22" r="8" fill="none" stroke="#f59e0b" strokeWidth="1.5" opacity="0.7" />}
                  {hasMains(2) && <circle cx="62" cy="22" r="8" fill="none" stroke="#f59e0b" strokeWidth="1.5" opacity="0.7" />}
                  {hasMains(0) && <circle cx="20" cy="50" r="8" fill="none" stroke="#f59e0b" strokeWidth="1.5" opacity="0.7" />}
                  {hasMains(3) && <circle cx="80" cy="50" r="8" fill="none" stroke="#f59e0b" strokeWidth="1.5" opacity="0.7" />}
                </svg>
              </div>
            </div>
            {noisyElectrodes.length > 0 && (
              <div style={{
                margin: '12px 0 0 0',
                padding: '10px 12px',
                borderRadius: '8px',
                background: 'rgba(245, 158, 11, 0.08)',
                border: '1px solid var(--warning)',
                fontSize: '0.75rem',
                lineHeight: 1.5,
              }}>
                <strong style={{ color: 'var(--warning)', display: 'block', marginBottom: '3px' }}>
                  ⚡ Mains hum on {noisyElectrodes.map((ch) => CHANNEL_LABELS[ch].split(' ')[0]).join(', ')}
                </strong>
                These electrodes are picking up far more mains interference than brain
                signal. Amplitude alone looks fine, so this would otherwise pass unnoticed
                — and it makes those channels close to unusable.
                <br />
                <strong>Fix:</strong> dampen the pads slightly, clear hair from underneath,
                and reseat so they sit on bare skin.
              </div>
            )}
            {channelQualities.includes('noise') && noisyElectrodes.length === 0 && (
              <p style={{ margin: '12px 0 0 0', fontSize: '0.75rem', color: 'var(--warning)', fontStyle: 'italic' }}>
                ⚠️ High noise detected! Clear hair behind ears and wipe forehead.
              </p>
            )}
            {channelQualities.includes('disconnected') && isConnected && (
              <p style={{ margin: '12px 0 0 0', fontSize: '0.75rem', color: 'var(--danger)', fontStyle: 'italic' }}>
                ⚠️ Some sensors have no contact. Make sure headband is snug.
              </p>
            )}
          </section>

          {/* Calibration Panel */}
          <section className="glass-card metric-card">
            <h3 className="card-title">2. Baseline Calibration</h3>
            
            <div className="text-center" style={{ margin: '20px 0' }}>
              {calState === 'idle' && (
                <>
                  <p style={{ fontSize: '0.85rem', color: 'var(--text-secondary)' }}>
                    Calibrate your brain waves to establish your focused vs. relaxed state baseline.
                  </p>
                  <button 
                    disabled={!isConnected} 
                    onClick={runCalibration}
                    style={{ width: '100%', marginTop: '10px' }}
                  >
                    Start 30s Calibration
                  </button>
                </>
              )}

              {calState === 'relax' && (
                <div className="flex flex-col items-center">
                  <span style={{ fontSize: '0.8rem', color: 'var(--primary)', fontWeight: 'bold', textTransform: 'uppercase' }}>Phase 1: Relax</span>
                  <p style={{ fontSize: '1rem', margin: '4px 0 12px 0' }}>Close eyes and clear your mind...</p>
                  <div style={{ fontSize: '3rem', fontWeight: '800', color: 'var(--primary)' }}>{calTimeLeft}s</div>
                </div>
              )}

              {calState === 'focus' && (
                <div className="flex flex-col items-center">
                  <span style={{ fontSize: '0.8rem', color: 'var(--success)', fontWeight: 'bold', textTransform: 'uppercase' }}>Phase 2: Focus</span>
                  <p style={{ fontSize: '1rem', margin: '4px 0 12px 0' }}>Focus intensely on a single point...</p>
                  <div style={{ fontSize: '3rem', fontWeight: '800', color: 'var(--success)' }}>{calTimeLeft}s</div>
                </div>
              )}

              {calState === 'done' && (
                <div className="text-center">
                  <span style={{ color: 'var(--success)', fontWeight: 'bold', display: 'block', marginBottom: '8px' }}>✓ Calibration Completed!</span>
                  <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '10px', fontSize: '0.8rem', background: 'rgba(255,255,255,0.02)', padding: '10px', borderRadius: '8px' }}>
                    <div>
                      <div style={{ color: 'var(--text-secondary)' }}>Baseline (Mid)</div>
                      <div style={{ fontSize: '1rem', fontWeight: 'bold' }}>{baseline.toFixed(3)}</div>
                    </div>
                    <div>
                      <div style={{ color: 'var(--text-secondary)' }}>Half Range</div>
                      <div style={{ fontSize: '1rem', fontWeight: 'bold' }}>{halfRange.toFixed(3)}</div>
                    </div>
                  </div>
                  <button 
                    onClick={runCalibration}
                    style={{ width: '100%', marginTop: '12px', background: 'rgba(255,255,255,0.05)', fontSize: '0.85rem' }}
                  >
                    Recalibrate
                  </button>
                </div>
              )}
            </div>
          </section>

          <CuedProtocol onCue={handleCue} disabled={!isConnected} />

        </div>

        {/* Right Hand Column: Live Visualizations & Brain Metrics */}
        <div className="flex flex-col gap-4">
          
          {/* Main Dial & Score Dashboard */}
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1.2fr', gap: '16px' }}>
            
            {/* Live Meditation Drive Dial */}
            <section className="glass-card metric-card items-center justify-between" style={{ padding: '24px' }}>
              <h3 className="card-title">Live Drive</h3>
              
              <div className="dial-container" style={{ margin: '16px 0' }}>
                <svg width="180" height="180" viewBox="0 0 180 180">
                  {/* Background Circle */}
                  <circle
                    cx="90"
                    cy="90"
                    r={radius}
                    fill="transparent"
                    stroke="rgba(255,255,255,0.05)"
                    strokeWidth="10"
                  />
                  {/* Gauge Arc */}
                  <circle
                    cx="90"
                    cy="90"
                    r={radius}
                    fill="transparent"
                    stroke={normalizedDrive >= 0 ? 'var(--success)' : 'var(--primary)'}
                    strokeWidth="10"
                    strokeDasharray={circumference}
                    strokeDashoffset={strokeDashoffset}
                    strokeLinecap="round"
                    transform="rotate(-90 90 90)"
                    style={{ transition: 'stroke-dashoffset 0.1s ease-out, stroke 0.3s' }}
                  />
                </svg>
                <div className="dial-value">
                  <span className="dial-score" style={{ color: normalizedDrive >= 0 ? 'var(--success)' : 'var(--primary)' }}>
                    {normalizedDrive >= 0 ? '+' : ''}{normalizedDrive.toFixed(2)}
                  </span>
                  <div className="dial-label">Score</div>
                </div>
              </div>

              <span style={{ fontSize: '0.8rem', color: 'var(--text-secondary)', textAlign: 'center' }}>
                {normalizedDrive > 0.4 ? '🔥 High Focus' : normalizedDrive < -0.4 ? '🌊 Deep Calm' : '⚖ Neutral'}
              </span>
            </section>

            {/* Cognitive Biomarkers / Spectral Breakdown */}
            <section className="glass-card metric-card">
              <h3 className="card-title">Spectral Powers</h3>
              
              <div className="band-bars-container">
                {Object.entries(bandPowersState).map(([bandName, value]) => {
                  const percent = (value / totalBandPower) * 100;
                  return (
                    <div className="band-bar-row" key={bandName}>
                      <span className="band-bar-label">{bandName}</span>
                      <div className="band-bar-outer">
                        <div 
                          className="band-bar-inner" 
                          style={{ 
                            width: `${percent}%`,
                            background: bandName === 'alpha' ? 'var(--success)' : bandName === 'beta' ? 'var(--primary)' : 'var(--text-secondary)'
                          }} 
                        />
                      </div>
                      <span className="band-bar-value">{percent.toFixed(0)}%</span>
                    </div>
                  );
                })}
              </div>

              <div style={{ marginTop: '20px', borderTop: '1px solid var(--card-border)', paddingTop: '12px', display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '10px' }}>
                <div>
                  <span style={{ fontSize: '0.7rem', color: 'var(--text-secondary)', display: 'block' }}>Ratio (Beta/Theta)</span>
                  <span style={{ fontSize: '1.2rem', fontWeight: 'bold' }}>
                    {(bandPowersState.beta / (bandPowersState.theta || 1)).toFixed(2)}
                  </span>
                </div>
                <div>
                  <span style={{ fontSize: '0.7rem', color: 'var(--text-secondary)', display: 'block' }}>Raw Focus Score</span>
                  <span style={{ fontSize: '1.2rem', fontWeight: 'bold', color: 'var(--primary)' }}>
                    {focusScore.toFixed(2)}
                  </span>
                </div>
              </div>
            </section>

          </div>

          {/* Real-time Oscilloscope Waves */}
          <section className="glass-card metric-card" style={{ flexGrow: 1 }}>
            <div className="flex justify-between items-center">
              <h3 className="card-title">Raw EEG Channels (Microvolts)</h3>
              {isMock && <span style={{ fontSize: '0.75rem', background: '#3b82f622', color: 'var(--primary)', padding: '2px 8px', borderRadius: '4px', border: '1px solid var(--primary-glow)' }}>SIMULATOR RUNNING</span>}
            </div>
            
            <div className="wave-visualizer">
              <canvas 
                ref={canvasRef} 
                width="600" 
                height="220" 
                style={{ width: '100%', height: '100%', display: 'block' }} 
              />
            </div>
          </section>

        </div>

      </div>
    </div>
  );
}

export default App;
