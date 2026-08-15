# LED Battle Modes — Design

**Date:** 2026-08-15
**Status:** Approved
**Project:** brain (Muse 2 EEG)

## Goal

Give the host a general framework for driving the LED strip: pick the players,
pick what mental state is being targeted, pick a game, and tune how hard it is —
all from the dashboard, with a simulated strip showing exactly what the real one
is doing.

The current LED path is a single hardcoded mapping: seat `p1`'s focus score onto
a blue-to-red colour ramp. There is no notion of a metric, a mode, a second
player, or a game. This design replaces that mapping with a pipeline whose
pieces can each be swapped, and delivers the first game on top of it.

## What exists today, and what is missing

| Piece | Today |
|---|---|
| Live score | Phone computes `log10(β) − log10(θ)`, calibrates it to a drive in `[-1,+1]`, streams at 30 Hz |
| Band powers | Computed on the phone at 4 Hz, **discarded unless recording** (`App.tsx:601`) |
| Snapshot | Carries `raw`, `normalized`, `calibrating` — no bands |
| Calibration | 15 s relax + 15 s focus on the tablet, yields `baseline`/`halfRange` for beta/theta only |
| Strip | One colour for the whole strip, from one seat |

Three things are missing outright: live band powers on the Pi, per-metric
calibration, and any concept of game state.

## Decisions

| Question | Decision |
|---|---|
| Where the game runs | The Pi. The strip must work with no dashboard open. |
| Simulator fidelity | The Pi renders pixels and ships them in the snapshot; the dashboard displays them. One renderer, so no drift. |
| Live bands | Channel-averaged five bands added to the existing 30 Hz telemetry message. |
| Recording path | Unchanged — per-channel bands still go in the raw batch. |
| Normalisation | The existing relax/focus ceremony, generalised to all metrics. |
| Who computes drive | The Pi, from live bands plus stored calibration. |
| Calibration trigger | Automatic on connect; re-triggerable by the host. |
| Momentum | Exponential lag toward a signal-driven target; `tau = 0` gives instant. |
| Bars retreating | Yes. Momentum is signed, so a player below threshold loses ground. |
| Config ownership | The Pi. Echoed in the snapshot so dashboards agree and a refresh is free. |

## The metric pipeline

### Live bands

The phone already holds channel-averaged band powers in `bandPowersState`,
updated every 250 ms. It adds them to the telemetry message it already sends at
30 Hz:

```json
{ "playerId": "p1", "rawScore": …, "normalizedScore": …,
  "isCalibrating": false,
  "bands": { "delta": …, "theta": …, "alpha": …, "beta": …, "gamma": … } }
```

Five extra floats on an existing message — no new message type and no new rate.
The raw batch path that feeds recordings is untouched and keeps shipping full
per-channel bands.

On the Pi the bands land in `_states[seat]["bands"]` and ride out in the
snapshot. They get the same `_is_finite_number` validation as the rest of the
telemetry: `powerByBand` can produce `NaN` or `Infinity`, and `JSON.stringify`
turns both into `null`.

Note that liveness is currently "the state dict changed" (`server.py:236`). A
five-float band block makes states differ more readily. This does not break the
frozen-headset detector — a frozen source still produces byte-identical bands —
but it does make the detector slightly more sensitive, which is the safe
direction.

### Calibration

The ceremony is unchanged for the wearer: 15 s relax, 15 s focus. What changes
is what gets accumulated. Today the tablet collects `rawFocus` samples; instead
it collects band-power samples and sends the two averages:

```json
{ "event": "calibration_complete",
  "relaxBands": { "delta": …, "theta": …, "alpha": …, "beta": …, "gamma": … },
  "focusBands": { "delta": …, "theta": …, "alpha": …, "beta": …, "gamma": … } }
```

The Pi derives, for every metric `m`:

```
baseline_m  = ½ (m(relaxBands) + m(focusBands))
halfRange_m = |½ (m(focusBands) − m(relaxBands))|
if halfRange_m < 0.05: halfRange_m = 1.0
drive_m     = clamp((m(bands) − baseline_m) / halfRange_m, -1, +1)
```

One ceremony calibrates every metric. Switching the dropdown mid-session needs
no recalibration, and adding a metric later is a Pi-only change — the tablets
never learn the list.

The degenerate-range guard matters more here than it did for one metric: a
metric that barely moves between relax and focus would otherwise divide by
almost nothing and pin to ±1 on noise.

Calibration runs automatically once a headset is paired and streaming. The
Recalibrate button stays, and the host can trigger it remotely.

### Metrics

All in log space, so ratios are symmetric and consistent with the existing
focus score.

| Id | Value | Note |
|---|---|---|
| `beta_theta_high` | `log10(β) − log10(θ)` | default; today's focus score |
| `beta_theta_low` | negated | |
| `alpha_beta_high` | `log10(α) − log10(β)` | |
| `alpha_beta_low` | negated | |
| `alpha_high` | `log10(α)` | |
| `alpha_low` | negated | |

For alpha metrics the relax/focus contrast runs backwards — alpha typically
falls when concentrating, so `focusMean < relaxMean`. The `abs()` in
`halfRange` handles the magnitude and the high/low direction handles which end
wins, so the maths is correct; the consequence is that "high alpha" is a game
won by relaxing. That is intended, and worth knowing when picking a mode for a
crowd.

## The game engine

### Momentum

Per player, per tick, with `dt` from an injected clock:

```
target    = gain × (drive − threshold)
momentum += (target − momentum) × (1 − exp(−dt / tau))
momentum  = clamp(momentum, ±max_momentum)
```

`tau = 0` collapses to `momentum = target`, i.e. instant response. Larger `tau`
gives build-up and coasting: a player who stops sees their bar glide to a halt
rather than freeze. This is the same exponential-lag shape as `Smoother` in
`led_driver.py`.

Momentum is signed. Below threshold a player loses ground.

### Battle mode 1

Two players: one blue on the left end, one green on the right. `N` is the pixel
count, `W = 5` the interface width.

**Phase `approach`.** Each length integrates its own momentum, floored at 0 so a
struggling player sits at their end rather than going negative:

```
length_i = max(0, length_i + momentum_i × dt)
```

Blue occupies `[0, length_blue)`, green occupies `[N − length_green, N)`, and
the gap between them is dark.

**Phase `contest`** begins when `length_blue + length_green ≥ N`. The boundary
seeds once, at `x = clamp(length_blue, W/2, N − W/2)`, so whoever got further
starts with the interface nearer their opponent. From then on only the net
momentum moves it:

```
x += (momentum_blue − momentum_green) × dt
```

The two lengths stop being independent state and derive from `x`; blue is
`[0, x − W/2)`, the white interface is `[x − W/2, x + W/2)`, green is
`[x + W/2, N)`. Keeping one counter rather than two removes any chance of them
drifting out of agreement.

Two consequences of that handover are deliberate. The interface has to occupy
`W` pixels that previously belonged to the bars, and seeding `x` at the meeting
point makes each bar give up `W/2` — a symmetric 2–3 pixel step at the exact
moment the white band appears, which reads as the event it is rather than as a
glitch. And the transition is **one-way**: once in contact the strip stays full,
because `x` is the only state and no gap can reopen. Both players retreating
moves the interface, it does not pull the bars apart.

**Phase `won`** fires when the interface reaches either end — `x ≤ W/2` (green
wins) or `x ≥ N − W/2` (blue wins). The whole strip flashes the winner's colour
at 2 Hz until the host presses Reset.

### Sensitivity

| Knob | Default | Range | Effect |
|---|---|---|---|
| `gain` | 8 px/s | 0–40 | ground gained per unit of drive |
| `threshold` | 0.0 | -1–+1 | drive that must be exceeded to gain ground |
| `tau` | 2.0 s | 0–10 | momentum lag; 0 is instant |
| `max_momentum` | 15 px/s | 1–60 | speed cap |

`gain` is expressed in pixels per second per unit of drive so it stays
interpretable when the strip length changes. At these defaults on a 144-pixel
strip, a player holding ~0.3 average drive covers their half in roughly half a
minute, giving a game of a couple of minutes.

### Edge cases

| Situation | Behaviour |
|---|---|
| A player goes stale or disconnects mid-game | Their drive is treated as `0`, so their momentum decays toward zero through the normal `tau` lag and they stop gaining ground. The game continues and the strip keeps rendering. No pause, no forfeit — a dropout mid-party should cost that player the initiative, not stop the show. |
| Host changes metric mid-game | Allowed, effective on the next tick. Momentum and bar positions are preserved; only the drive feeding them changes. Every metric is already calibrated, so nothing needs to re-run. |
| Host changes sensitivity mid-game | Allowed, effective on the next tick. `tau` changes alter the lag from that point on without discarding accumulated momentum. |
| A selected player has never calibrated | Their calibration defaults to `baseline = 0`, `halfRange = 1`, i.e. the raw log ratio. The dashboard marks them uncalibrated and Start warns, but does not block — an uncalibrated game is still better than a blocked one at a party. |
| Fewer than two players selected in `battle1` | Start is refused with a message. The mode dropdown offers `solo` instead. |

## Rendering

`strip_render.py` is a pure function from game state to a list of `(r, g, b)`.

Brightness scales with `|momentum| / max_momentum` but is floored at **0.15**.
Without a floor, a player at rest has an invisible bar and all the length
information is lost — which is exactly what you most want to read from across a
dark space.

Colours: blue `(0, 0, 255)`, green `(0, 255, 0)`, interface white
`(255, 255, 255)`, background off.

The pixel list goes into the snapshot as a flat integer array. At 144 pixels
that is ~1.7 KB per snapshot and ~17 KB/s at 10 Hz — negligible on a LAN, and
readable in devtools, which is worth more than the bytes base64 would save.
Revisit only if the strip gets much longer.

The dashboard's simulated strip renders that array directly. No colour logic
exists in the browser, so the simulator cannot disagree with the strip.

## Host controls

On the dashboard:

- Checkboxes on the seat cards select 1–2 players
- Mode dropdown: `solo`, `battle1`
- Metric dropdown: the six entries above
- Four sensitivity sliders
- Start and Reset
- Per player: Recalibrate, and direct `baseline` / `halfRange` nudges
- The simulated strip along the bottom

Endpoints, all Pi-authoritative:

| Route | Purpose |
|---|---|
| `POST /api/lights/config` | mode, metric, players, sensitivity |
| `POST /api/lights/start` | begin a game |
| `POST /api/lights/reset` | clear back to an empty strip |
| `POST /api/seats/{seat}/recalibrate` | push a recalibrate request down the player socket |
| `PATCH /api/seats/{seat}/calibration` | adjust `baseline` / `halfRange` directly |

Recalibrating remotely reuses the existing pattern for pushing a message to
players (`_announce_recording`).

`battle1` requires exactly two selected players. With one, the mode list offers
`solo`, which reproduces today's colour behaviour.

Snapshot additions:

```json
{ "seats": [ { …, "bands": {…}, "drive": 0.42 } ],
  "lights": {
    "mode": "battle1",
    "metric": "beta_theta_high",
    "players": ["p1", "p2"],
    "sensitivity": { "gain": 8.0, "threshold": 0.0, "tau": 2.0, "max_momentum": 15.0 },
    "game": { "phase": "contest", "winner": null,
              "momentum": { "p1": 3.2, "p2": -1.1 }, "boundary": 72.5 },
    "pixels": [0, 0, 255, …] } }
```

## Module layout

| Module | Responsibility | Depends on |
|---|---|---|
| `metrics.py` | band powers → named metric value; calibration maths | nothing |
| `battle.py` | momentum, phases, win conditions | injected clock |
| `strip_render.py` | game state → pixels | nothing |
| `led_driver.py` | transport; `WledStrip.show_pixels(list)` | socket |
| `server.py` | config endpoints, one `tick()` in the broadcast loop | the above |

`build_dnrgb_packets` generalises to take per-pixel data; today's solid-colour
call becomes a one-line special case of it. `FocusLight` is superseded by
`solo` mode.

## Testing

`metrics.py`, `battle.py` and `strip_render.py` are pure with injected clocks,
so they get ordinary table-driven pytest in the shape of `test_cue_protocol.py`:
phase transitions, retreat below threshold, momentum lag at several `tau`
values including 0, win conditions at both ends, and pixel layout at each phase.

Server tests cover the config endpoints, calibration derivation from
`relaxBands`/`focusBands`, and that pixels reach the snapshot. The existing 207
tests stay green.

## Sequencing

This is a large spec. The order below keeps each step independently verifiable
and gets something visible on screen early, so the game can be tuned by eye
rather than by reading numbers.

1. Fix the `[-1, +1]` clamp bug — standalone, ships before any of this
2. `WledStrip.show_pixels()` and per-pixel `build_dnrgb_packets`
3. `metrics.py` — metric values and calibration derivation, pure
4. Live bands: phone telemetry → `_states` → snapshot
5. `strip_render.py` — pure, with a hardcoded game state
6. Simulated strip on the dashboard — makes steps 2–5 visible
7. `battle.py` — the game, pure
8. Game tick in the broadcast loop, config endpoints, `lights` in the snapshot
9. Host controls: players, mode, metric, sensitivity, start/reset
10. Calibration on connect, remote recalibrate, host calibration nudges

Steps 1–6 produce a working simulated strip driven by real telemetry before any
game logic exists. Step 7 onward is then tunable against something you can see.

## Out of scope

- More than two players, or team play
- Battle modes beyond the first
- Per-person segments in `solo` mode
- Persisting host config across server restarts
- Recording game outcomes to disk

## Related fix

`normalizedScore` is a signed drive in `[-1, +1]`, but `focus_to_rgb` clamps to
`[0, 1]` — so the entire relaxed half of the range collapses to blue and a
neutral wearer reads as maximally relaxed. This is live on PR #14. It should be
fixed as its own small change rather than waiting for this design to land.
