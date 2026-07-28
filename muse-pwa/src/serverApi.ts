// The page and the sockets share an origin, so nothing here is configured by
// hand: whatever served this build also terminates the websockets.

export type SeatState = {
  id: string;
  connected: boolean;
  raw: number;
  normalized: number;
  calibrating: boolean;
};

export type RecordingState = {
  active: boolean;
  label: string;
};

export type HubState = {
  type: 'state';
  seats: SeatState[];
  recording: RecordingState;
};

const wsScheme = () => (window.location.protocol === 'https:' ? 'wss' : 'ws');

export const playerSocketUrl = (seat: string) =>
  `${wsScheme()}://${window.location.host}/ws/${seat}`;

export const observerSocketUrl = () =>
  `${wsScheme()}://${window.location.host}/ws/observe`;

/** The seat roster is owned by the server so adding a seat is a server flag. */
export async function fetchSeats(): Promise<string[]> {
  const response = await fetch('/api/seats');
  if (!response.ok) throw new Error(`/api/seats returned ${response.status}`);
  const body = await response.json();
  return body.seats ?? [];
}

export async function startSession(label: string) {
  const response = await fetch('/api/session/start', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ label }),
  });
  if (!response.ok) throw new Error(await response.text());
  return response.json();
}

export async function stopSession() {
  const response = await fetch('/api/session/stop', { method: 'POST' });
  if (!response.ok) throw new Error(await response.text());
  return response.json();
}
