import { useEffect, useRef, useState } from 'react';
import {
  HubState,
  SeatState,
  observerSocketUrl,
  startSession,
  stopSession,
} from './serverApi';
import './App.css';
import './Dashboard.css';

const EMPTY: HubState = {
  type: 'state',
  seats: [],
  recording: { active: false, label: '' },
};

/** Map a drive in [-1, +1] onto a 0-100% position across the bar. */
const drivePercent = (normalized: number) =>
  ((Math.max(-1, Math.min(1, normalized)) + 1) / 2) * 100;

const driveColor = (seat: SeatState) => {
  if (!seat.connected) return 'var(--text-muted)';
  // A stale reading is not wrong so much as old; colouring it like a live
  // value is what makes a dead seat look alive.
  if (seat.stale) return 'var(--text-muted)';
  if (seat.calibrating) return 'var(--warning)';
  return seat.normalized >= 0 ? 'var(--success)' : 'var(--primary)';
};

const statusLabel = (seat: SeatState) => {
  if (!seat.connected) return 'Empty';
  if (seat.stale) return 'No data';
  return seat.calibrating ? 'Calibrating' : 'Live';
};

const statusColor = (seat: SeatState) => {
  if (!seat.connected) return 'var(--text-muted)';
  if (seat.stale) return 'var(--warning)';
  return 'var(--success)';
};

function SeatCard({ seat }: { seat: SeatState }) {
  const percent = drivePercent(seat.normalized);
  const dotColor = statusColor(seat);

  return (
    <section
      className={`seat-card ${!seat.connected ? 'seat-empty' : ''} ${seat.stale ? 'seat-stale' : ''}`}
    >
      <header className="seat-head">
        <span className="seat-name">{seat.id.toUpperCase()}</span>
        <span className="seat-status">
          <span
            className="status-dot"
            style={{
              background: dotColor,
              boxShadow: seat.connected && !seat.stale ? '0 0 8px var(--success-glow)' : 'none',
            }}
          />
          {statusLabel(seat)}
        </span>
      </header>

      <div className="seat-score" style={{ color: driveColor(seat) }}>
        {seat.connected ? `${seat.normalized >= 0 ? '+' : ''}${seat.normalized.toFixed(2)}` : '--'}
      </div>

      {seat.stale && (
        <div className="seat-stale-note">
          connected but silent — last value shown
        </div>
      )}

      <div className="drive-track">
        <div className="drive-midline" />
        <div
          className="drive-marker"
          style={{ left: `${percent}%`, background: driveColor(seat) }}
        />
      </div>

      <div className="drive-legend">
        <span>calm</span>
        <span>focus</span>
      </div>

      <div className="seat-raw">
        raw {seat.connected ? seat.raw.toFixed(2) : '--'}
      </div>
    </section>
  );
}

export default function Dashboard() {
  const [state, setState] = useState<HubState>(EMPTY);
  const [linkUp, setLinkUp] = useState(false);
  const [label, setLabel] = useState('session');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const socketRef = useRef<WebSocket | null>(null);

  useEffect(() => {
    let reconnectTimer: number;
    let closed = false;

    const connect = () => {
      const socket = new WebSocket(observerSocketUrl());
      socketRef.current = socket;

      socket.onopen = () => setLinkUp(true);

      socket.onmessage = (event) => {
        try {
          setState(JSON.parse(event.data));
        } catch {
          // A truncated frame is not worth tearing the view down over; the
          // next snapshot is a tenth of a second away.
        }
      };

      socket.onclose = () => {
        setLinkUp(false);
        if (!closed) reconnectTimer = window.setTimeout(connect, 2000);
      };
    };

    connect();

    return () => {
      closed = true;
      clearTimeout(reconnectTimer);
      socketRef.current?.close();
    };
  }, []);

  const recording = state.recording.active;

  const toggleSession = async () => {
    setBusy(true);
    setError(null);
    try {
      if (recording) {
        await stopSession();
      } else {
        await startSession(label.trim() || 'session');
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  // A silent seat is not live, whatever its socket says.
  const liveCount = state.seats.filter((s) => s.connected && !s.stale).length;
  const staleCount = state.seats.filter((s) => s.stale).length;

  return (
    <div className="dash-container">
      <header className="dash-header glass-card">
        <div>
          <h1 className="dash-title">Muse Operator</h1>
          <span className="dash-subtitle">
            {liveCount} of {state.seats.length} seats live
            {staleCount > 0 && (
              <span style={{ color: 'var(--warning)' }}>
                {' '}· {staleCount} silent
              </span>
            )}
          </span>
        </div>

        <div className="dash-controls">
          <span className="dash-link">
            <span
              className="status-dot"
              style={{ background: linkUp ? 'var(--success)' : 'var(--danger)' }}
            />
            {linkUp ? 'Connected' : 'Reconnecting'}
          </span>

          <input
            className="dash-label"
            value={label}
            onChange={(e) => setLabel(e.target.value)}
            placeholder="session label"
            disabled={recording}
          />

          <button
            onClick={toggleSession}
            disabled={busy}
            className={recording ? 'rec-stop' : 'rec-start'}
          >
            {recording ? `Stop "${state.recording.label}"` : 'Start recording'}
          </button>
        </div>
      </header>

      {error && <div className="dash-error glass-card">{error}</div>}

      {recording && (
        <div className="rec-banner">
          <span className="rec-pip" />
          Recording &ldquo;{state.recording.label}&rdquo;
        </div>
      )}

      <div className="seat-grid">
        {state.seats.map((seat) => (
          <SeatCard key={seat.id} seat={seat} />
        ))}
      </div>

      {state.seats.length === 0 && (
        <p className="dash-waiting">Waiting for the server…</p>
      )}
    </div>
  );
}
