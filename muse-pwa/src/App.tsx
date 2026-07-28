import { useState, useEffect, useRef } from 'react';
import { connectMuse, Muse } from 'web-muse';
import {
  SAMPLE_RATE,
  WINDOW_SIZE,
  CHANNELS,
  sanitizeAndInterpolate,
  calculatePowerSpectrum,
  powerByBand,
  BandPowers
} from './utils/dsp';
import './App.css';

// Channel labels for Muse 2
const CHANNEL_LABELS = ['TP9 (Left Ear)', 'AF7 (Left Forehead)', 'AF8 (Right Forehead)', 'TP10 (Right Ear)'];
const CHANNEL_COLORS = ['#3b82f6', '#10b981', '#f59e0b', '#ec4899']; // Blue, Green, Yellow, Pink

// The page and the socket share an origin, so the host is never configured by
// hand: whatever served this build also terminates the websocket.
const socketUrlFor = (player: string) => {
  const scheme = window.location.protocol === 'https:' ? 'wss' : 'ws';
  return `${scheme}://${window.location.host}/ws/${player}`;
};

// Web Bluetooth only exists in a secure context. Served over plain HTTP from a
// LAN address, navigator.bluetooth is simply undefined — so check up front and
// explain it, rather than letting the pair button throw something cryptic.
const bluetoothAvailable = window.isSecureContext && 'bluetooth' in navigator;

function App() {
  // Device & Connection State
  const [museDevice, setMuseDevice] = useState<Muse | null>(null);
  const [isConnected, setIsConnected] = useState(false);
  const [isMock, setIsMock] = useState(false);
  const [battery, setBattery] = useState<number | null>(null);
  
  // WebSocket State
  const [playerId, setPlayerId] = useState<'p1' | 'p2'>('p1');
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

  // Canvas Reference
  const canvasRef = useRef<HTMLCanvasElement | null>(null);

  // 1. Maintain WebSocket Connection
  useEffect(() => {
    let reconnectTimeout: number;

    const connectWS = () => {
      if (wsRef.current) {
        wsRef.current.close();
      }

      setWsStatus('connecting');
      const wsUrl = socketUrlFor(playerId);
      console.log(`Connecting to WebSocket: ${wsUrl}`);
      
      const socket = new WebSocket(wsUrl);
      wsRef.current = socket;

      socket.onopen = () => {
        setWsStatus('connected');
        console.log('WebSocket connection established.');
      };

      socket.onclose = () => {
        setWsStatus('disconnected');
        console.log('WebSocket connection lost. Reconnecting in 3s...');
        reconnectTimeout = window.setTimeout(connectWS, 3000);
      };

      socket.onerror = (err) => {
        console.error('WebSocket Error:', err);
      };
    };

    connectWS();

    return () => {
      if (wsRef.current) {
        wsRef.current.close();
      }
      clearTimeout(reconnectTimeout);
    };
  }, [playerId]);

  // 2. Headset Data Loop (draining the MuseCircularBuffer)
  useEffect(() => {
    if (!isConnected || !museDevice) {
      setChannelQualities(['disconnected', 'disconnected', 'disconnected', 'disconnected']);
      return;
    }

    let dspInterval: number;
    let wsStreamInterval: number;
    let mockDataGeneratorInterval: number;
    let animationFrameId: number;

    // A. Read samples from Muse Circular Buffer into our local sliding window
    const pollBuffers = () => {
      // Draining the buffers
      for (let ch = 0; ch < CHANNELS; ch++) {
        const buffer = museDevice.eeg[ch];
        if (!buffer) continue;

        let sample: number | null;
        while ((sample = buffer.read()) !== null) {
          // Push to FFT sliding window (512 samples)
          eegBuffersRef.current[ch].push(sample);
          if (eegBuffersRef.current[ch].length > WINDOW_SIZE) {
            eegBuffersRef.current[ch].shift();
          }

          // Push to UI scrolling waveforms buffer (200 samples)
          scrollBuffersRef.current[ch].push(sample);
          if (scrollBuffersRef.current[ch].length > 200) {
            scrollBuffersRef.current[ch].shift();
          }
        }
      }

      // Read battery level if available
      if (museDevice.batteryLevel !== null) {
        setBattery(museDevice.batteryLevel);
      }

      animationFrameId = requestAnimationFrame(pollBuffers);
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

    // Start draining buffers
    pollBuffers();

    // C. DSP Calculations: FFT + Band Powers (Runs at 4Hz / every 250ms)
    dspInterval = window.setInterval(() => {
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

      for (let ch = 0; ch < CHANNELS; ch++) {
        const rawData = eegBuffersRef.current[ch];
        const sanitized = sanitizeAndInterpolate(rawData);
        const spectrum = calculatePowerSpectrum(sanitized);
        const powers = powerByBand(spectrum);
        channelPowers.push(powers);
      }

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
    wsStreamInterval = window.setInterval(() => {
      if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
        // Calculate dynamic drive normalized score based on current focus
        let drive = (focusScore - baseline) / halfRange;
        drive = Math.max(-1.0, Math.min(1.0, drive));
        
        // Push state up
        setNormalizedDrive(drive);

        wsRef.current.send(JSON.stringify({
          playerId: playerId,
          rawScore: focusScore,
          normalizedScore: drive,
          calibrationPhase: calState,
          isCalibrating: calState === 'relax' || calState === 'focus'
        }));
      }
    }, 1000 / 30);

    return () => {
      cancelAnimationFrame(animationFrameId);
      clearInterval(dspInterval);
      clearInterval(wsStreamInterval);
      clearInterval(mockDataGeneratorInterval);
    };
  }, [isConnected, museDevice, isMock, focusScore, baseline, halfRange, playerId, calState]);

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

              <div className="flex flex-col">
                <label style={{ fontSize: '0.75rem', color: 'var(--text-secondary)', marginBottom: '4px' }}>Player Seat</label>
                <select
                  value={playerId}
                  onChange={(e) => setPlayerId(e.target.value as 'p1' | 'p2')}
                >
                  <option value="p1">Player 1 (Left)</option>
                  <option value="p2">Player 2 (Right)</option>
                </select>
                <span style={{ fontSize: '0.7rem', color: 'var(--text-secondary)', marginTop: '6px' }}>
                  Streaming to {socketUrlFor(playerId)}
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
                <button onClick={handleDisconnect} style={{ marginTop: '8px', background: 'rgba(239, 68, 68, 0.1)', borderColor: 'var(--danger)', color: 'var(--danger)' }}>
                  Disconnect {isMock ? 'Mock' : 'Muse'}
                </button>
              )}
            </div>
          </section>

          {/* Headset Fit Panel */}
          <section className="glass-card metric-card">
            <h3 className="card-title">Headset Fit Status</h3>
            <div className="flex justify-between items-center gap-4" style={{ marginTop: '16px' }}>
              <div style={{ flexGrow: 1, display: 'flex', flexDirection: 'column', gap: '8px', fontSize: '0.8rem', color: 'var(--text-secondary)' }}>
                <div className="flex items-center gap-2">
                  <span style={{ width: '8px', height: '8px', borderRadius: '50%', background: getQualityColor(channelQualities[0]) }} />
                  <span>TP9 (Left Ear): <strong style={{ color: getQualityColor(channelQualities[0]) }}>{channelQualities[0]}</strong></span>
                </div>
                <div className="flex items-center gap-2">
                  <span style={{ width: '8px', height: '8px', borderRadius: '50%', background: getQualityColor(channelQualities[1]) }} />
                  <span>AF7 (Left Forehead): <strong style={{ color: getQualityColor(channelQualities[1]) }}>{channelQualities[1]}</strong></span>
                </div>
                <div className="flex items-center gap-2">
                  <span style={{ width: '8px', height: '8px', borderRadius: '50%', background: getQualityColor(channelQualities[2]) }} />
                  <span>AF8 (Right Forehead): <strong style={{ color: getQualityColor(channelQualities[2]) }}>{channelQualities[2]}</strong></span>
                </div>
                <div className="flex items-center gap-2">
                  <span style={{ width: '8px', height: '8px', borderRadius: '50%', background: getQualityColor(channelQualities[3]) }} />
                  <span>TP10 (Right Ear): <strong style={{ color: getQualityColor(channelQualities[3]) }}>{channelQualities[3]}</strong></span>
                </div>
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
                  <circle cx="38" cy="22" r="5" fill={getQualityColor(channelQualities[1])} style={{ transition: 'fill 0.3s' }} />
                  <circle cx="62" cy="22" r="5" fill={getQualityColor(channelQualities[2])} style={{ transition: 'fill 0.3s' }} />
                  
                  {/* Ear Electrodes (TP9 & TP10) */}
                  <circle cx="20" cy="50" r="5" fill={getQualityColor(channelQualities[0])} style={{ transition: 'fill 0.3s' }} />
                  <circle cx="80" cy="50" r="5" fill={getQualityColor(channelQualities[3])} style={{ transition: 'fill 0.3s' }} />
                </svg>
              </div>
            </div>
            {channelQualities.includes('noise') && (
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
