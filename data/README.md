# Recorded sessions

`latest/` holds the most recent published recording. It is replaced wholesale
each time `scripts/publish-session.sh` runs, so the front page always shows the
newest thing rather than an archive. Every session stays on the Pi, so nothing
is lost by republishing.

The bulk streams are gzipped to keep the repository small. Everything in
`analysis/` reads either form, and `gunzip` gets you plain CSV for a spreadsheet.

## Files

Every row carries `t`, seconds from the start of the recording, and `seat`,
which identifies the person (`p1`…`p4`) so one file holds everyone.

| File | Columns after `t, seat` | Rate |
|---|---|---|
| `eeg.csv` | `TP9, AF7, AF8, TP10` — microvolts | 256 Hz |
| `ppg.csv` | `ppg1, ppg2, ppg3` — pulse | ~64 Hz |
| `acc.csv` | `x, y, z` — acceleration | ~52 Hz |
| `gyro.csv` | `x, y, z` — rotation | ~52 Hz |
| `bands.csv` | `channel, delta, theta, alpha, beta, gamma` | 4 Hz |
| `telemetry.csv` | `focus_score, normalized, calibrating` | 30 Hz |
| `cues.csv` | `phase, eyes, task, trial` | per event |
| `gaps.csv` | `stream, resumed_at` | per event |
| `seats.csv` | `event` — connect/disconnect | per event |
| `meta.json` | duration, row counts, error count | — |

Electrodes are `TP9` behind the left ear, `AF7` and `AF8` on the forehead, and
`TP10` behind the right ear.

## Reading it

```python
from analysis.session import Session

s = Session("data/latest")
print(s)                       # label, duration, trial count

eeg = s.clean_eeg()            # mains notched, band-passed 1-45 Hz
raw = s.eeg()                  # untouched, if you would rather filter yourself

for trial in s.trials():       # only for sessions using the cued protocol
    print(trial.eyes, trial.task, trial.eeg().shape)
```

## Things worth knowing before trusting a number

**Check the mains ratio first.** `SUMMARY.md` reports it per channel. Above
about 100x, that electrode picked up more mains hum than brain signal — usually
a dry ear pad. The hum sits at 50 or 60 Hz, outside every band of interest, so
`clean_eeg()` removes it; but a channel that was swamped had poor contact, and
poor contact costs signal quality beyond the hum itself.

**`focus_score` is not raw data.** It is `log10(beta) − log10(theta)` averaged
across all four electrodes, one number per 250 ms. If two channels were noisy,
half its input was noise. Prefer recomputing from `eeg.csv`, which is why the
raw signal is recorded at all.

**Check `gaps.csv` is empty.** Each row marks a point where the phone reported
losing samples — normally a locked screen, since browsers throttle background
tabs to about 1 Hz. An empty file means the recording is continuous.

**Timestamps within a batch are reconstructed.** Samples arrive in batches of
about 100 ms and are spaced by the known sample rate, so spacing inside a batch
is exact while batch boundaries carry a few milliseconds of network jitter.
Fine for within-person analysis; not accurate enough to claim millisecond
synchrony between two people.

**An uncued session has no labels.** If `cues.csv` is missing, nothing recorded
what the person was doing, and no amount of analysis recovers it. Sessions
recorded with the cued protocol have a condition attached to every sample.
