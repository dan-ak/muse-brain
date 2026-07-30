# Raw Signal Capture — Design

**Date:** 2026-07-30
**Status:** Approved
**Project:** brain (Muse 2 EEG)

## Goal

Record what the headsets actually measure, not just the one number the current
demo happens to use.

Today a session saves `log10(beta) − log10(theta)`, averaged across all four
electrodes, once per 250 ms. For a 39-second session that is ~156 numbers out of
~40,000 available EEG samples, and PPG and head motion never leave the browser
at all. The reduction is irreversible: per-channel signals, other frequency
bands, artifacts, and sub-250 ms timing are gone.

Since the end use is still undecided, capturing the full signal now is the only
choice that does not foreclose it later. Disk is cheap; a session you failed to
record is gone.

## What the hardware offers

Per headset, via `web-muse`'s circular buffers:

| Stream | Channels | Rate | Notes |
|--------|----------|------|-------|
| EEG | 4 (TP9, AF7, AF8, TP10) | 256 Hz | the primary signal |
| PPG | 3 | ~64 Hz | **MU-03 only**; MU-02 has no PPG hardware |
| Accelerometer | 3 axes | ~52 Hz | head position, nods, stillness |
| Gyroscope | 3 axes | ~52 Hz | head rotation |
| Band powers | 5 per channel | 4 Hz | already computed for the focus score, then discarded |

Roughly 1,530 samples/sec per headset. Batched as JSON that is ~15 KB/s per
seat, so ~45 KB/s for three — negligible on the LAN. Written one row per
timestamp with channels as columns, three seats produce ~1,100 rows/sec, about
400 MB per recorded hour.

## Decisions

| Question | Decision |
|----------|----------|
| When to stream | Only while a session is recording. The server tells clients. |
| Batching | Client batches at 10 Hz; one websocket message carries all streams. |
| Timestamps | Server back-dates within a batch from the known sample rate. |
| Alignment | Accelerometer and gyroscope stay separate streams. |
| Writes | `SessionRecorder` switches to buffered writes with periodic flush. |
| Sample loss | Counted by the client, surfaced on the dashboard. |

## Streaming only while recording

Raw streaming costs phone battery and CPU, and most of the time nobody is
recording. So the server sends `{"type": "recording", "active": bool}` to every
player socket on connect and whenever a session starts or stops, and clients
stream raw only while it is true.

Sending on connect as well as on change matters: a phone that joins mid-session
would otherwise sit silent until the next start, and its data would be missing
from a recording that looks complete.

## Timestamps

Samples are batched, so they cannot all carry their arrival time. The client
does not share a clock with the server, and synchronising one is more machinery
than this needs.

Instead the server back-dates each batch: for `N` samples at rate `R` arriving
at session time `T`, sample `i` gets `T − (N − 1 − i)/R`. Intra-batch spacing is
then exact, and batch-to-batch error is bounded by network latency — a few
milliseconds on a LAN, against a 3.9 ms EEG sample interval.

This is good enough for within-seat analysis and for coarse cross-seat
comparison. It is *not* good enough to claim millisecond-accurate synchrony
between two people's EEG; that would need a real clock-sync handshake, and is
deliberately out of scope until something actually requires it.

## Streams written

New `/pwa/*` routes in `SessionRecorder.FIXED`, each carrying a seat column so
one file holds every player:

| Address | File | Columns after `t` |
|---------|------|-------------------|
| `/pwa/eeg` | `eeg.csv` | `seat, TP9, AF7, AF8, TP10` |
| `/pwa/ppg` | `ppg.csv` | `seat, ppg1, ppg2, ppg3` |
| `/pwa/acc` | `acc.csv` | `seat, x, y, z` |
| `/pwa/gyro` | `gyro.csv` | `seat, x, y, z` |
| `/pwa/bands` | `bands.csv` | `seat, channel, delta, theta, alpha, beta, gamma` |

The existing `/muse/*` OSC routes write files of the same name without a seat
column. A session only ever comes from one stack, and `meta.json` records both
`source` and the exact columns per file, so the files stay self-describing.

`telemetry.csv` keeps the focus score exactly as now, so nothing already
validated regresses. Its misleadingly named `raw` column becomes
`focus_score` — it is a derived scalar, and calling it `raw` alongside genuinely
raw EEG would be actively misleading.

## Buffered writes

`SessionRecorder` currently opens CSVs line-buffered, one write syscall per row.
That is fine at today's 120 rows/sec and wasteful at 1,100. It moves to a 64 KB
buffer with an explicit `flush()`, which the server calls every couple of
seconds from the broadcast loop it already runs.

Periodic flushing rather than relying on `stop()` alone means a crash or a power
cut costs a couple of seconds of data instead of the whole session — a real
consideration for a Pi running off a battery in a desert.

## Sample loss

Every `web-muse` buffer holds 256 samples, which at 256 Hz is exactly **one
second** of EEG headroom. If the drain loop stalls longer than that, samples are
silently overwritten.

The current drain runs on `requestAnimationFrame`, which stops entirely in a
hidden tab. So a locked screen does not merely throttle the focus score, it
loses raw samples outright. Draining moves to a 100 ms interval, giving a 10×
margin, and the client counts samples it believes it lost by observing a full
buffer at drain time.

That count travels with each batch and is surfaced per seat on the dashboard.
Silent data loss is far worse than visible data loss: an operator can move a
phone or unlock a screen, but only if they know.

## Error handling

A malformed or partial raw batch is skipped like any other bad frame, without
dropping the connection. Unknown stream names inside a batch are ignored rather
than erroring, so a newer client cannot break an older server. Recording
failures never propagate to a player socket — delivering telemetry still matters
more than recording it.

## Testing

Unit tests for batch back-dating (exact intra-batch spacing, correct ordering),
each stream landing in the right file with the right columns, partial batches
where PPG is absent (the MU-02 case), malformed batches being ignored, drop
counts accumulating and appearing in the snapshot, and raw frames being
discarded when no session is recording.

Integration tests over a real websocket for the recording-state notification on
connect and on change, and for a full batch round-trip landing on disk.

## Out of scope

Clock synchronisation between seats, binary framing (JSON is ~2.5× larger than
packed floats but 45 KB/s is not a problem worth solving yet), and any analysis
of the captured data.
