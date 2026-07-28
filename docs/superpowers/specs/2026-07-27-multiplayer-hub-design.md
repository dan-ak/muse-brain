# Multiplayer Hub — Design

**Date:** 2026-07-27
**Status:** Approved
**Project:** brain (Muse 2 EEG)

## Goal

Let three or more people wear Muses at once, show every connected headset on one
screen, and write the session to disk. Today `server.py` accepts exactly two
hardcoded seats, has no interface beyond a JSON health endpoint, and discards
every sample it receives.

## Decisions

| Question | Decision |
|----------|----------|
| Seat count | Configurable via `--seats N`, default 4. IDs are `p1`…`pN`. |
| Seat discovery | The PWA fetches the roster from the server rather than hardcoding it. |
| Dashboard transport | A read-only observer websocket broadcasting full state at 10 Hz. |
| Dashboard location | A route in the existing React app, not a separate bundle. |
| Recording | Reuse `SessionRecorder` by adding `/pwa/*` routes to its `FIXED` table. |
| Session control | Operator-driven from the dashboard, over `POST /api/session/*`. |

## Seats

`PlayerHub` already takes a seat list, so the change is mostly plumbing: a
`--seats N` flag builds `p1`…`pN`, and `GET /api/seats` exposes the roster so
the PWA can populate its selector instead of hardcoding two options.

A fixed roster is deliberate. Accepting arbitrary seat identifiers would mean a
typo on someone's phone silently creates a phantom player that appears on the
dashboard and in the recording. With a known roster an unrecognised seat is
rejected at connection time, which is a far better failure. The default of 4
leaves a spare seat for the three headsets so adding a fourth person does not
mean reconfiguring the Pi in the dark.

## Observer websocket

`GET /ws/observe` accepts read-only observers and pushes a full state snapshot
at 10 Hz — the dashboard is for human eyes and does not need the 30 Hz the
players stream at.

Snapshots are whole-state rather than deltas. State is small (a handful of
floats per seat), so sending it whole means a dashboard that connects late, or
reconnects after a laptop sleeps, is immediately correct with no replay logic.

Observers are strictly read-only: frames received on an observer socket are
ignored. The dashboard drives the session over HTTP instead, so there is one
path for commands rather than two.

A single broadcast task owns the send loop. Sends to a dead observer are
discarded and the observer dropped, so one closed laptop lid cannot stall the
loop for everyone else.

## Recording

`SessionRecorder` already handles session directories, timestamping, CSV
routing, `meta.json`, and thread-safe state transitions. It routes by address
through a `FIXED` table, so the websocket path is served by adding entries
rather than by writing a second recorder:

| Address | File | Columns |
|---------|------|---------|
| `/pwa/telemetry` | `telemetry.csv` | `seat, raw, normalized, calibrating` |
| `/pwa/calibration` | `calibration.csv` | `seat, baseline, half_range` |
| `/pwa/seat` | `seats.csv` | `seat, event` |

The `/pwa/` prefix keeps these honestly distinct from real OSC addresses coming
off a headset. Adding entries is additive: the OSC path never emits these
addresses, and no existing test asserts on the table's contents.

Calibration results are currently parsed and thrown away. They are worth
recording, because a normalized score is meaningless later without the baseline
and half-range it was derived from.

Seat connect and disconnect events are recorded too, so a gap in the telemetry
can be explained after the fact — a headset that fell off looks exactly like a
phone that locked itself unless the transition was logged.

### Blocking I/O

`SessionRecorder` writes line-buffered CSV from the calling thread, and here
that thread is the event loop. At 30 Hz across four seats this is roughly 120
small writes per second totalling a few KB — well inside what the loop absorbs,
and not worth a writer thread. Revisit if seat count grows by an order of
magnitude.

## Session control

`POST /api/session/start` (optional JSON `label`) and `POST /api/session/stop`.
Recording state appears in the broadcast so every connected dashboard reflects
it, not just the one that pressed the button.

Starting an already-running session returns the existing directory rather than
erroring, matching `SessionRecorder.start`'s existing behavior.

## Dashboard

A route in the existing React app. The server serves the app shell for
`/dashboard` so a refresh on that URL works, and the client picks the view from
`window.location.pathname`. Sharing a bundle keeps one build and one set of
types; the operator view and the player view are different enough visually that
nothing else is shared.

It shows every seat with its live normalized drive, connection state, and
calibration state, with unoccupied seats dimmed rather than hidden so the roster
reads as a fixed set of places. Session controls sit alongside.

## Error handling

An unknown seat is rejected at connect, as now. Observer sockets that fail to
receive are dropped from the set. Session endpoints return the recorder's actual
state rather than assuming success. A recorder that raises never breaks a player
connection — telemetry delivery matters more than recording it.

## Testing

Extending the existing pytest suite: roster construction from `--seats`,
`/api/seats`, observer connect and snapshot shape, observer isolation from
player state, broadcast reflecting recording state, session start and stop
lifecycle, recorded rows landing in the right CSVs with the right columns, seat
events recorded on connect and disconnect, and a recorder failure not
propagating to the player socket.

## Out of scope

Authentication on the dashboard (the network is a closed LAN), historical
playback, and unifying the focus metric between the Python and TypeScript
implementations.
