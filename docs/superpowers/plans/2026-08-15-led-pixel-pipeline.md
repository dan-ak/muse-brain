# LED Pixel Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the single-colour LED path into a per-pixel pipeline fed by live band powers, ending with a simulated strip on the dashboard that shows exactly what the real strip is doing.

**Architecture:** Three new pure modules — `metrics.py` (band powers → named metric → calibrated drive) and `strip_render.py` (state → pixel list) — plus a generalised `led_driver.py` whose DNRGB packets carry per-pixel data instead of one repeated colour. The phone adds channel-averaged band powers to the telemetry message it already sends at 30 Hz; the Pi stores them per seat, renders pixels in the existing 10 Hz broadcast loop, drives WLED, and puts the same pixel array in the snapshot for the dashboard to display.

**Tech Stack:** Python 3 (stdlib only — no new dependencies), aiohttp, pytest; React + TypeScript for the dashboard. Run tests with `QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest`.

**Spec:** `docs/superpowers/specs/2026-08-15-led-battle-modes-design.md`

## Global Constraints

- No new Python dependencies. The LED path is stdlib `socket` only.
- The existing 207 tests must stay green after every task.
- Recording path is untouched: per-channel bands keep going out in the raw batch.
- WLED UDP realtime, protocol `4` (DNRGB), port `21324`, timeout byte `2`. Protocol `3` is DRGBW and silently renders garbage.
- Max `489` pixels per datagram; packets must stay ≤ 1472 bytes.
- Colours: relaxed `(0, 0, 255)`, concentrated `(255, 0, 0)`, neutral is magenta `(255, 0, 255)`.
- `normalizedScore` / drive is a **signed** value in `[-1, +1]`. `-1` relaxed, `0` baseline, `+1` focused.
- Degenerate calibration guard: `half_range < 0.05` → `1.0`.
- Sends never raise. Transport failures are reported by return value only.
- Run the full suite from the repo root, not from a subdirectory.

---

## File Structure

| File | Responsibility |
|------|----------------|
| `metrics.py` (new) | `METRICS`, `metric_value`, `calibration_from`, `calibrate_all`, `drive`. Pure maths, no I/O. |
| `strip_render.py` (new) | `focus_to_rgb`, `scale`, `render_solo`. State → pixels. No network, no clock. |
| `led_driver.py` (modify) | `build_dnrgb_packets` takes a pixel list; `solid()`; `WledStrip.show_pixels`. Colour ramp moves out. |
| `server.py` (modify) | Validate and store `bands`; add `lights.pixels` to the broadcast payload. |
| `muse-pwa/src/App.tsx` (modify) | Send channel-averaged bands on the 30 Hz telemetry message. |
| `muse-pwa/src/serverApi.ts` (modify) | `BandPowers` type; `bands` on `SeatState`; `lights` on `HubState`. |
| `muse-pwa/src/Dashboard.tsx` (modify) | `SimulatedStrip` component. |
| `muse-pwa/src/Dashboard.css` (modify) | Strip styling. |
| `test_metrics.py` (new) | Unit tests for `metrics.py`. |
| `test_strip_render.py` (new) | Unit tests for `strip_render.py`. |
| `test_led_driver.py` (modify) | Signed drive; per-pixel packets. |
| `test_server.py` (modify) | Band validation, bands in snapshot, pixels in payload. |

---

### Task 1: Fix the signed-drive clamp

`focus_to_rgb` clamps to `[0, 1]`, but the drive it receives is `[-1, +1]`. The entire relaxed half collapses to blue and a neutral wearer reads as maximally relaxed. This is a live bug; it ships on its own.

**Files:**
- Modify: `led_driver.py` (`focus_to_rgb`)
- Modify: `test_led_driver.py`
- Modify: `README.md`

**Interfaces:**
- Consumes: nothing
- Produces: `focus_to_rgb(drive: float) -> tuple[int, int, int]` where `-1.0` is blue, `0.0` is magenta, `+1.0` is red

- [ ] **Step 1: Write the failing test**

Add to `test_led_driver.py`:

```python
def test_neutral_drive_is_magenta_not_blue():
    # The drive is signed: 0.0 is the calibrated baseline, not "relaxed".
    # Clamping to [0, 1] made the whole negative half read as fully relaxed.
    assert focus_to_rgb(0.0) == (255, 0, 255)


def test_fully_relaxed_is_blue():
    assert focus_to_rgb(-1.0) == (0, 0, 255)


def test_relaxed_half_of_the_range_is_not_all_blue():
    assert focus_to_rgb(-0.5) != focus_to_rgb(-1.0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest test_led_driver.py -k "neutral_drive or fully_relaxed or relaxed_half" -v`
Expected: FAIL — `assert (0, 0, 255) == (255, 0, 255)` for the neutral case.

- [ ] **Step 3: Write minimal implementation**

In `led_driver.py`, replace `focus_to_rgb`:

```python
def focus_to_rgb(drive: float) -> tuple[int, int, int]:
    """-1.0 -> blue (relaxed), 0.0 -> magenta (baseline), +1.0 -> red.

    The drive arriving from the phone is signed: it is
    ``(score - baseline) / halfRange`` clamped to [-1, +1], so zero means "at
    your own calibrated baseline", not "relaxed". Clamping it to [0, 1] threw
    away the entire relaxed half of the range.

    Walks the hue circle from 240 to 360 degrees at full saturation and value,
    which on that arc is exactly two linear segments: blue to magenta, then
    magenta to red.
    """
    t = (min(1.0, max(-1.0, float(drive))) + 1.0) / 2.0
    if t <= 0.5:
        return (round(255 * t * 2), 0, 255)
    return (255, 0, round(255 * (1 - (t - 0.5) * 2)))
```

- [ ] **Step 4: Update the existing tests that encoded the old range**

In `test_led_driver.py`, replace these tests:

```python
def test_relaxed_is_blue():
    assert focus_to_rgb(-1.0) == (0, 0, 255)


def test_concentrated_is_red():
    assert focus_to_rgb(1.0) == (255, 0, 0)


def test_midpoint_is_magenta():
    assert focus_to_rgb(0.0) == (255, 0, 255)


def test_ramp_never_dims_in_the_middle():
    # The reason for interpolating around the hue circle instead of lerping RGB:
    # a straight blue->red lerp passes through (127, 0, 127), so the middle of
    # the scale reads as "the lights are broken" rather than as a middle value.
    for i in range(-100, 101):
        assert max(focus_to_rgb(i / 100.0)) == 255


def test_ramp_has_no_green():
    for i in range(-100, 101):
        assert focus_to_rgb(i / 100.0)[1] == 0


def test_ramp_is_monotonic_in_red():
    reds = [focus_to_rgb(i / 100.0)[0] for i in range(-100, 101)]
    assert reds == sorted(reds)


def test_out_of_range_scores_are_clamped():
    assert focus_to_rgb(-1.5) == focus_to_rgb(-1.0)
    assert focus_to_rgb(1.5) == focus_to_rgb(1.0)
```

Then fix the four `FocusLight` tests that used `0.0` to mean "relaxed" — change each `normalized=0.0` to `normalized=-1.0`:

```python
def test_a_relaxed_wearer_turns_the_strip_blue():
    strip, clk = FakeStrip(), FakeClock(0.0)
    light = FocusLight(strip, seat="p1", tau_s=1.5, clock_fn=clk)
    light.update(_snapshot(normalized=-1.0))
    assert strip.shown == [(0, 0, 255)]


def test_reconnecting_does_not_fade_from_the_pre_dropout_colour():
    strip, clk = FakeStrip(), FakeClock(0.0)
    light = FocusLight(strip, seat="p1", tau_s=1.5, clock_fn=clk)
    light.update(_snapshot(normalized=1.0))       # red
    clk.t = 1.0
    light.update(_snapshot(connected=False))      # dropout
    clk.t = 2.0
    light.update(_snapshot(normalized=-1.0))      # back, and relaxed
    assert strip.shown[-1] == (0, 0, 255)


def test_smoothing_applies_across_successive_updates():
    strip, clk = FakeStrip(), FakeClock(0.0)
    light = FocusLight(strip, seat="p1", tau_s=1.5, clock_fn=clk)
    light.update(_snapshot(normalized=-1.0))
    clk.t = 0.1
    light.update(_snapshot(normalized=1.0))
    assert strip.shown[0] == (0, 0, 255)
    assert strip.shown[1] != (255, 0, 0)   # eased, not snapped
```

- [ ] **Step 5: Run the full suite**

Run: `QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q`
Expected: PASS, 210 tests (207 + 3 new).

- [ ] **Step 6: Update the README**

In `README.md`, in the Lights section, replace the first paragraph:

```markdown
An LED strip can show one wearer's state to the room: **blue when relaxed, red
when concentrated**, with magenta at their calibrated baseline. The score is a
signed drive in `[-1, +1]`, so the middle of the ramp is a real reading rather
than an absence of one.
```

- [ ] **Step 7: Commit**

```bash
git add led_driver.py test_led_driver.py README.md
git commit -m "fix: map the full signed drive range onto the colour ramp"
```

---

### Task 2: Per-pixel DNRGB packets

**Files:**
- Modify: `led_driver.py` (`build_dnrgb_packets`, `WledStrip`)
- Modify: `test_led_driver.py`

**Interfaces:**
- Consumes: nothing
- Produces:
  - `build_dnrgb_packets(pixels: list[tuple[int,int,int]], timeout_s: int = 2) -> list[bytes]`
  - `solid(rgb: tuple[int,int,int], count: int) -> list[tuple[int,int,int]]`
  - `WledStrip.show_pixels(pixels) -> bool`
  - `WledStrip.show(rgb) -> bool` (unchanged behaviour, now a wrapper)

- [ ] **Step 1: Write the failing test**

Add to `test_led_driver.py`:

```python
def test_packets_carry_per_pixel_colours():
    pixels = [(1, 2, 3), (4, 5, 6), (7, 8, 9)]
    packets = build_dnrgb_packets(pixels, timeout_s=2)
    assert len(packets) == 1
    assert packets[0][:4] == bytes([4, 2, 0, 0])
    assert packets[0][4:] == bytes([1, 2, 3, 4, 5, 6, 7, 8, 9])


def test_solid_builds_a_uniform_pixel_list():
    assert solid((9, 8, 7), 3) == [(9, 8, 7), (9, 8, 7), (9, 8, 7)]


def test_per_pixel_run_splits_across_datagrams():
    pixels = [(1, 2, 3)] * (DNRGB_MAX_PIXELS + 10)
    packets = build_dnrgb_packets(pixels, timeout_s=2)
    assert len(packets) == 2
    assert len(packets[0][4:]) == DNRGB_MAX_PIXELS * 3
    assert (packets[1][2] << 8) | packets[1][3] == DNRGB_MAX_PIXELS


def test_show_pixels_sends_the_given_colours():
    sock = FakeSocket()
    strip = WledStrip("10.0.0.5", count=3, sock=sock)
    strip.show_pixels([(1, 2, 3), (4, 5, 6), (7, 8, 9)])
    payload, _ = sock.sent[0]
    assert payload[4:] == bytes([1, 2, 3, 4, 5, 6, 7, 8, 9])


def test_empty_pixel_list_sends_nothing():
    sock = FakeSocket()
    strip = WledStrip("10.0.0.5", count=0, sock=sock)
    assert strip.show_pixels([]) is True
    assert sock.sent == []
```

Update the import at the top of `test_led_driver.py` to add `solid`:

```python
from led_driver import (
    DNRGB_MAX_PIXELS,
    WLED_REALTIME_PORT,
    FocusLight,
    Smoother,
    WledStrip,
    build_dnrgb_packets,
    focus_to_rgb,
    solid,
)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest test_led_driver.py -q`
Expected: FAIL at collection — `ImportError: cannot import name 'solid'`.

- [ ] **Step 3: Write minimal implementation**

In `led_driver.py`, replace `build_dnrgb_packets` and add `solid`:

```python
def solid(rgb, count: int) -> list[tuple[int, int, int]]:
    """One colour repeated — the whole-strip case of a pixel list."""
    return [tuple(rgb)] * count


def build_dnrgb_packets(pixels, timeout_s: int = DEFAULT_TIMEOUT_S) -> list[bytes]:
    """A pixel list as WLED DNRGB datagrams, split at the per-packet limit."""
    packets = []
    start = 0
    total = len(pixels)
    while start < total:
        run = min(DNRGB_MAX_PIXELS, total - start)
        header = bytes([DNRGB_PROTOCOL, timeout_s, (start >> 8) & 0xFF, start & 0xFF])
        body = bytearray()
        for r, g, b in pixels[start:start + run]:
            body += bytes((r, g, b))
        packets.append(header + bytes(body))
        start += run
    return packets
```

Then replace `WledStrip.show` with a pair:

```python
    def show_pixels(self, pixels) -> bool:
        """Paint an explicit pixel list. True if every datagram went out.

        A failed datagram does not abandon the rest: on a strip long enough to
        need several, stopping early would leave its head on the new colour and
        its tail on the old one.
        """
        sent = True
        for packet in build_dnrgb_packets(pixels, self._timeout_s):
            try:
                self._sock.sendto(packet, self._addr)
            except OSError:
                sent = False
        return sent

    def show(self, rgb) -> bool:
        """Paint the whole strip one colour."""
        return self.show_pixels(solid(rgb, self._count))
```

- [ ] **Step 4: Update the three tests that called the old signature**

In `test_led_driver.py`, replace:

```python
def test_packet_carries_protocol_and_timeout_header():
    packets = build_dnrgb_packets(solid((255, 0, 0), 1), timeout_s=2)
    assert len(packets) == 1
    assert packets[0][:4] == bytes([4, 2, 0, 0])   # 4 = DNRGB, 3 would be DRGBW


def test_packet_repeats_the_colour_for_every_pixel():
    packets = build_dnrgb_packets(solid((10, 20, 30), 3), timeout_s=2)
    assert packets[0][4:] == bytes([10, 20, 30] * 3)


def test_long_strips_are_split_across_packets():
    packets = build_dnrgb_packets(solid((1, 2, 3), DNRGB_MAX_PIXELS + 10), timeout_s=2)
    assert len(packets) == 2
    assert len(packets[0][4:]) == DNRGB_MAX_PIXELS * 3
    assert len(packets[1][4:]) == 10 * 3


def test_continuation_packet_carries_its_start_index():
    packets = build_dnrgb_packets(solid((1, 2, 3), DNRGB_MAX_PIXELS + 1), timeout_s=2)
    start = (packets[1][2] << 8) | packets[1][3]
    assert start == DNRGB_MAX_PIXELS


def test_packet_fits_in_a_single_datagram():
    packets = build_dnrgb_packets(solid((1, 2, 3), 1000), timeout_s=2)
    for packet in packets:
        assert len(packet) <= 1472
```

- [ ] **Step 5: Run the full suite**

Run: `QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q`
Expected: PASS, 215 tests.

- [ ] **Step 6: Commit**

```bash
git add led_driver.py test_led_driver.py
git commit -m "feat: carry per-pixel colours in DNRGB packets"
```

---

### Task 3: `metrics.py`

**Files:**
- Create: `metrics.py`
- Test: `test_metrics.py`

**Interfaces:**
- Consumes: nothing
- Produces:
  - `METRICS: dict[str, tuple[str, str, int]]` — id → (label, ratio id, sign)
  - `DEFAULT_METRIC = "beta_theta_high"`
  - `metric_value(bands: dict, metric_id: str) -> float`
  - `calibration_from(relax_bands: dict, focus_bands: dict, metric_id: str) -> tuple[float, float]`
  - `calibrate_all(relax_bands: dict, focus_bands: dict) -> dict[str, tuple[float, float]]`
  - `drive(bands: dict, metric_id: str, calibration: tuple[float, float] | None = None) -> float`
  - `NEUTRAL_CALIBRATION = (0.0, 1.0)`

A `bands` dict has the five float keys `delta`, `theta`, `alpha`, `beta`, `gamma`.

- [ ] **Step 1: Write the failing test**

Create `test_metrics.py`:

```python
import pytest

from metrics import (
    DEFAULT_METRIC,
    METRICS,
    NEUTRAL_CALIBRATION,
    calibrate_all,
    calibration_from,
    drive,
    metric_value,
)


def bands(delta=1.0, theta=1.0, alpha=1.0, beta=1.0, gamma=1.0):
    return {"delta": delta, "theta": theta, "alpha": alpha, "beta": beta, "gamma": gamma}


def test_default_metric_is_the_existing_focus_score():
    assert DEFAULT_METRIC == "beta_theta_high"


def test_all_six_metrics_are_registered():
    assert set(METRICS) == {
        "beta_theta_high", "beta_theta_low",
        "alpha_beta_high", "alpha_beta_low",
        "alpha_high", "alpha_low",
    }


def test_beta_theta_matches_the_phone_formula():
    # log10(beta) - log10(theta); the phone computes exactly this.
    value = metric_value(bands(beta=100.0, theta=1.0), "beta_theta_high")
    assert value == pytest.approx(2.0, abs=1e-4)


def test_low_direction_is_the_negation_of_high():
    b = bands(beta=100.0, theta=1.0)
    assert metric_value(b, "beta_theta_low") == pytest.approx(
        -metric_value(b, "beta_theta_high"), abs=1e-9)


def test_alpha_beta_is_a_log_ratio():
    value = metric_value(bands(alpha=10.0, beta=1.0), "alpha_beta_high")
    assert value == pytest.approx(1.0, abs=1e-4)


def test_zero_power_does_not_blow_up():
    # powerByBand can return 0 for a dead channel; log10(0) is -inf.
    value = metric_value(bands(alpha=0.0, beta=0.0), "alpha_beta_high")
    assert value == pytest.approx(0.0, abs=1e-9)


def test_calibration_centres_between_relax_and_focus():
    baseline, half_range = calibration_from(
        bands(beta=1.0, theta=1.0), bands(beta=100.0, theta=1.0), "beta_theta_high")
    assert baseline == pytest.approx(1.0, abs=1e-4)
    assert half_range == pytest.approx(1.0, abs=1e-4)


def test_calibrated_drive_spans_minus_one_to_plus_one():
    relax, focus = bands(beta=1.0, theta=1.0), bands(beta=100.0, theta=1.0)
    cal = calibration_from(relax, focus, "beta_theta_high")
    assert drive(relax, "beta_theta_high", cal) == pytest.approx(-1.0, abs=1e-4)
    assert drive(focus, "beta_theta_high", cal) == pytest.approx(1.0, abs=1e-4)


def test_drive_is_clamped_beyond_the_calibrated_range():
    cal = calibration_from(
        bands(beta=1.0, theta=1.0), bands(beta=100.0, theta=1.0), "beta_theta_high")
    assert drive(bands(beta=10_000.0, theta=1.0), "beta_theta_high", cal) == 1.0


def test_degenerate_range_falls_back_rather_than_dividing_by_nothing():
    # A metric that barely moves between the two phases would otherwise divide
    # by almost nothing and pin the drive to +/-1 on noise.
    _, half_range = calibration_from(
        bands(alpha=1.0), bands(alpha=1.0), "alpha_high")
    assert half_range == 1.0


def test_alpha_contrast_runs_backwards_and_still_calibrates():
    # Alpha falls when concentrating, so focusMean < relaxMean. abs() handles
    # the magnitude; "high alpha" is then a game won by relaxing.
    relax, focus = bands(alpha=100.0), bands(alpha=1.0)
    cal = calibration_from(relax, focus, "alpha_high")
    assert drive(relax, "alpha_high", cal) == pytest.approx(1.0, abs=1e-4)
    assert drive(focus, "alpha_high", cal) == pytest.approx(-1.0, abs=1e-4)


def test_calibrate_all_covers_every_metric():
    cals = calibrate_all(bands(beta=1.0, theta=1.0), bands(beta=100.0, theta=1.0))
    assert set(cals) == set(METRICS)
    for baseline, half_range in cals.values():
        assert half_range > 0


def test_uncalibrated_drive_uses_the_raw_ratio():
    assert NEUTRAL_CALIBRATION == (0.0, 1.0)
    value = drive(bands(beta=10.0, theta=1.0), "beta_theta_high")
    assert value == pytest.approx(1.0, abs=1e-4)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest test_metrics.py -q`
Expected: FAIL at collection — `ModuleNotFoundError: No module named 'metrics'`.

- [ ] **Step 3: Write minimal implementation**

Create `metrics.py`:

```python
"""Named EEG metrics and the calibration that makes them comparable.

Pure maths: no I/O, no clock, no knowledge of seats or strips. A ``bands`` dict
holds the five channel-averaged band powers the phone computes every 250 ms.

Every metric is a log-space value so ratios are symmetric, and every metric
carries a direction, so "low alpha" is simply the negation of "high alpha" and
calibration works identically for both.
"""

from __future__ import annotations

import math

BAND_NAMES = ("delta", "theta", "alpha", "beta", "gamma")

# Matches the epsilon the phone uses, so beta/theta agrees with the score the
# tablet already displays.
_EPS = 1e-6


def _log(value) -> float:
    """log10 with a floor, so a dead channel reading 0 does not give -inf."""
    return math.log10(max(0.0, float(value)) + _EPS)


_RATIOS = {
    "beta_theta": lambda b: _log(b["beta"]) - _log(b["theta"]),
    "alpha_beta": lambda b: _log(b["alpha"]) - _log(b["beta"]),
    "alpha": lambda b: _log(b["alpha"]),
}

# id -> (label for the dropdown, ratio id, direction)
METRICS = {
    "beta_theta_high": ("High beta/theta (focus)", "beta_theta", +1),
    "beta_theta_low": ("Low beta/theta", "beta_theta", -1),
    "alpha_beta_high": ("High alpha/beta", "alpha_beta", +1),
    "alpha_beta_low": ("Low alpha/beta", "alpha_beta", -1),
    "alpha_high": ("High alpha", "alpha", +1),
    "alpha_low": ("Low alpha", "alpha", -1),
}

DEFAULT_METRIC = "beta_theta_high"

# Below this the relax and focus phases did not separate, so the range is noise.
MIN_HALF_RANGE = 0.05
FALLBACK_HALF_RANGE = 1.0

# What an uncalibrated player gets: the raw log ratio, unscaled.
NEUTRAL_CALIBRATION = (0.0, 1.0)


def metric_value(bands, metric_id: str) -> float:
    """The signed value of one metric for one set of band powers."""
    _, ratio_id, sign = METRICS[metric_id]
    return sign * _RATIOS[ratio_id](bands)


def calibration_from(relax_bands, focus_bands, metric_id: str) -> tuple[float, float]:
    """``(baseline, half_range)`` from the two calibration phases.

    Same shape as the tablet's existing calibration: the baseline is the
    midpoint of the two states and the half-range is half their separation, so
    a calibrated drive runs -1 at relax to +1 at focus.
    """
    relax = metric_value(relax_bands, metric_id)
    focus = metric_value(focus_bands, metric_id)
    baseline = 0.5 * (relax + focus)
    half_range = abs(0.5 * (focus - relax))
    if half_range < MIN_HALF_RANGE:
        half_range = FALLBACK_HALF_RANGE
    return baseline, half_range


def calibrate_all(relax_bands, focus_bands) -> dict[str, tuple[float, float]]:
    """Calibrate every metric from one ceremony."""
    return {
        metric_id: calibration_from(relax_bands, focus_bands, metric_id)
        for metric_id in METRICS
    }


def drive(bands, metric_id: str, calibration=None) -> float:
    """A metric as a signed drive in [-1, +1]."""
    baseline, half_range = calibration or NEUTRAL_CALIBRATION
    value = metric_value(bands, metric_id)
    return max(-1.0, min(1.0, (value - baseline) / half_range))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest test_metrics.py -q`
Expected: PASS, 13 tests.

- [ ] **Step 5: Run the full suite**

Run: `QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q`
Expected: PASS, 228 tests.

- [ ] **Step 6: Commit**

```bash
git add metrics.py test_metrics.py
git commit -m "feat: named EEG metrics with per-metric calibration"
```

---

### Task 4: Live bands from phone to snapshot

**Files:**
- Modify: `muse-pwa/src/App.tsx` (band ref + telemetry payload)
- Modify: `server.py` (`_blank_state`, `handle_message`, new `_clean_bands`)
- Modify: `muse-pwa/src/serverApi.ts`
- Modify: `test_server.py`

**Interfaces:**
- Consumes: `metrics.BAND_NAMES` from Task 3
- Produces: `seat["bands"]` in the snapshot — a dict of five floats, or `None` when absent or unusable

- [ ] **Step 1: Write the failing test**

Add to `test_server.py`, at the end of the file:

```python
class TestLiveBands:
    @async_test
    async def test_bands_reach_the_snapshot(self, static_dir):
        hub = PlayerHub(seat_ids=make_seat_ids(2))
        async with TestClient(TestServer(create_app(static_dir=static_dir, hub=hub))) as client:
            async with client.ws_connect("/ws/p1") as player:
                await player.send_json({
                    "normalizedScore": 0.5,
                    "bands": {"delta": 1.0, "theta": 2.0, "alpha": 3.0,
                              "beta": 4.0, "gamma": 5.0},
                })
                await _settle()

            seat = next(s for s in hub.snapshot()["seats"] if s["id"] == "p1")
            assert seat["bands"] == {"delta": 1.0, "theta": 2.0, "alpha": 3.0,
                                     "beta": 4.0, "gamma": 5.0}

    @async_test
    async def test_missing_bands_are_none(self, static_dir):
        hub = PlayerHub(seat_ids=make_seat_ids(2))
        async with TestClient(TestServer(create_app(static_dir=static_dir, hub=hub))) as client:
            async with client.ws_connect("/ws/p1") as player:
                await player.send_json({"normalizedScore": 0.5})
                await _settle()

            seat = next(s for s in hub.snapshot()["seats"] if s["id"] == "p1")
            assert seat["bands"] is None

    @async_test
    async def test_non_finite_bands_are_rejected_whole(self, static_dir):
        # powerByBand can produce NaN or Infinity, and JSON.stringify turns both
        # into null. One bad band makes the whole set untrustworthy.
        hub = PlayerHub(seat_ids=make_seat_ids(2))
        async with TestClient(TestServer(create_app(static_dir=static_dir, hub=hub))) as client:
            async with client.ws_connect("/ws/p1") as player:
                await player.send_json({
                    "normalizedScore": 0.5,
                    "bands": {"delta": 1.0, "theta": None, "alpha": 3.0,
                              "beta": 4.0, "gamma": 5.0},
                })
                await _settle()

            seat = next(s for s in hub.snapshot()["seats"] if s["id"] == "p1")
            assert seat["bands"] is None

    @async_test
    async def test_partial_band_sets_are_rejected(self, static_dir):
        hub = PlayerHub(seat_ids=make_seat_ids(2))
        async with TestClient(TestServer(create_app(static_dir=static_dir, hub=hub))) as client:
            async with client.ws_connect("/ws/p1") as player:
                await player.send_json({
                    "normalizedScore": 0.5,
                    "bands": {"delta": 1.0, "theta": 2.0},
                })
                await _settle()

            seat = next(s for s in hub.snapshot()["seats"] if s["id"] == "p1")
            assert seat["bands"] is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest test_server.py -k TestLiveBands -q`
Expected: FAIL — `KeyError: 'bands'`.

- [ ] **Step 3: Write the server implementation**

In `server.py`, add the import near the top, beside the existing `led_driver` import:

```python
from metrics import BAND_NAMES
```

Add a helper just after `_is_finite_number`:

```python
def _clean_bands(bands):
    """The five band powers as floats, or None if any is missing or unusable.

    Rejected as a set rather than per band: a metric is a ratio of two of
    these, so one bad value poisons any metric that touches it. Half a band
    set is not better than none.
    """
    if not isinstance(bands, dict):
        return None
    cleaned = {}
    for name in BAND_NAMES:
        value = bands.get(name)
        if not _is_finite_number(value):
            return None
        cleaned[name] = float(value)
    return cleaned
```

Change `_blank_state`:

```python
def _blank_state():
    return {"raw": 0.0, "normalized": 0.0, "calibrating": False, "bands": None}
```

In `handle_message`, extend the state dict:

```python
        try:
            state = {
                "raw": float(data.get("rawScore", 0.0)),
                "normalized": float(data.get("normalizedScore", 0.0)),
                "calibrating": bool(data.get("isCalibrating", False)),
                "bands": _clean_bands(data.get("bands")),
            }
        except (TypeError, ValueError):
            return IGNORED
```

- [ ] **Step 4: Run test to verify it passes**

Run: `QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest test_server.py -k TestLiveBands -q`
Expected: PASS, 4 tests.

- [ ] **Step 5: Send bands from the phone**

In `muse-pwa/src/App.tsx`, add a ref beside the other refs (near `calibrationSamplesRef`):

```tsx
  const bandPowersRef = useRef<BandPowers>({
    delta: 0, theta: 0, alpha: 0, beta: 0, gamma: 0
  });
```

In the DSP interval, immediately after `setBandPowersState(avgPowers);`, add:

```tsx
      bandPowersRef.current = avgPowers;
```

In the 30 Hz websocket send, add `bands` to the payload:

```tsx
        wsRef.current.send(JSON.stringify({
          playerId: playerIdRef.current,
          rawScore: score,
          normalizedScore: drive,
          calibrationPhase: phase,
          isCalibrating: phase === 'relax' || phase === 'focus',
          bands: bandPowersRef.current
        }));
```

- [ ] **Step 6: Add the dashboard types**

In `muse-pwa/src/serverApi.ts`, add above `SeatState`:

```ts
export type BandPowers = {
  delta: number;
  theta: number;
  alpha: number;
  beta: number;
  gamma: number;
};
```

and add the field to `SeatState`:

```ts
  /** Channel-averaged band powers, or null when the seat is not sending usable ones. */
  bands: BandPowers | null;
```

- [ ] **Step 7: Build the PWA**

Run: `cd muse-pwa && npm run build && cd ..`
Expected: build succeeds with no type errors.

- [ ] **Step 8: Run the full suite**

Run: `QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q`
Expected: PASS, 232 tests.

- [ ] **Step 9: Commit**

```bash
git add server.py test_server.py muse-pwa/src/App.tsx muse-pwa/src/serverApi.ts
git commit -m "feat: stream live band powers to the Pi"
```

---

### Task 5: `strip_render.py`

The colour ramp moves out of `led_driver.py` so that rendering and transport are separate concerns. `led_driver` keeps no module-level dependency on `strip_render`; its bring-up CLI imports locally.

**Files:**
- Create: `strip_render.py`
- Test: `test_strip_render.py`
- Modify: `led_driver.py` (remove `focus_to_rgb`, `RELAXED`, `CONCENTRATED`; local import in `_bring_up`)
- Modify: `test_led_driver.py` (import the ramp from its new home)

**Interfaces:**
- Consumes: nothing
- Produces:
  - `focus_to_rgb(drive: float) -> tuple[int, int, int]`
  - `scale(rgb: tuple[int,int,int], brightness: float) -> tuple[int,int,int]`
  - `render_solo(drive: float, count: int) -> list[tuple[int,int,int]]`
  - `RELAXED = (0, 0, 255)`, `CONCENTRATED = (255, 0, 0)`, `OFF = (0, 0, 0)`

- [ ] **Step 1: Write the failing test**

Create `test_strip_render.py`:

```python
from strip_render import (
    CONCENTRATED,
    OFF,
    RELAXED,
    focus_to_rgb,
    render_solo,
    scale,
)


def test_relaxed_is_blue():
    assert focus_to_rgb(-1.0) == RELAXED


def test_concentrated_is_red():
    assert focus_to_rgb(1.0) == CONCENTRATED


def test_baseline_is_magenta():
    assert focus_to_rgb(0.0) == (255, 0, 255)


def test_scale_dims_every_channel():
    assert scale((200, 100, 50), 0.5) == (100, 50, 25)


def test_scale_to_zero_is_off():
    assert scale((255, 255, 255), 0.0) == OFF


def test_scale_never_exceeds_a_byte():
    assert scale((255, 255, 255), 1.0) == (255, 255, 255)


def test_solo_paints_every_pixel_the_same():
    pixels = render_solo(1.0, 5)
    assert pixels == [CONCENTRATED] * 5


def test_solo_length_matches_the_strip():
    assert len(render_solo(0.0, 144)) == 144


def test_solo_with_no_pixels_is_empty():
    assert render_solo(0.0, 0) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest test_strip_render.py -q`
Expected: FAIL at collection — `ModuleNotFoundError: No module named 'strip_render'`.

- [ ] **Step 3: Write minimal implementation**

Create `strip_render.py`:

```python
"""Turn state into pixels. Pure: no network, no clock, no game rules.

The colour ramp is deliberately not a linear RGB interpolation. Lerping blue to
red passes through a half-brightness purple, so the middle of the scale reads as
a fault rather than as a middle value. Interpolating along the hue circle
instead (240 degrees to 360) keeps every point fully saturated and equally
bright: blue -> violet -> magenta -> red.
"""

from __future__ import annotations

RELAXED = (0, 0, 255)
CONCENTRATED = (255, 0, 0)
OFF = (0, 0, 0)


def focus_to_rgb(drive: float) -> tuple[int, int, int]:
    """-1.0 -> blue (relaxed), 0.0 -> magenta (baseline), +1.0 -> red.

    The drive is signed: zero means "at your own calibrated baseline", not
    "relaxed".
    """
    t = (min(1.0, max(-1.0, float(drive))) + 1.0) / 2.0
    if t <= 0.5:
        return (round(255 * t * 2), 0, 255)
    return (255, 0, round(255 * (1 - (t - 0.5) * 2)))


def scale(rgb, brightness: float) -> tuple[int, int, int]:
    """Dim a colour. Brightness is clamped to [0, 1]."""
    factor = min(1.0, max(0.0, float(brightness)))
    return tuple(round(channel * factor) for channel in rgb)


def render_solo(drive: float, count: int) -> list[tuple[int, int, int]]:
    """One wearer's state across the whole strip."""
    return [focus_to_rgb(drive)] * count
```

- [ ] **Step 4: Run test to verify it passes**

Run: `QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest test_strip_render.py -q`
Expected: PASS, 9 tests.

- [ ] **Step 5: Remove the ramp from `led_driver.py`**

Delete `RELAXED`, `CONCENTRATED` and `focus_to_rgb` from `led_driver.py`, and
replace the module docstring — it currently claims this module maps scores onto
a colour ramp, which is exactly what is moving out:

```python
"""Stream pixels to a WLED controller.

Transport only: framing a pixel list as WLED DNRGB datagrams and getting them
onto the wire. Colour and layout decisions live in ``strip_render``. Nothing
here can raise into the caller, because the caller is the loop that records the
EEG.
"""
```

In `FocusLight.update`, import the ramp locally so the transport module keeps no module-level dependency on the renderer:

```python
    def update(self, snapshot) -> tuple[int, int, int] | None:
        """Drive one frame. Returns the colour shown, or None if released."""
        from strip_render import focus_to_rgb

        score = self._score(snapshot)
        if score is None:
            self._smoother.reset()
            self._strip.release()
            return None
        rgb = focus_to_rgb(self._smoother.update(score))
        self._strip.show(rgb)
        return rgb
```

In `_bring_up`, replace the module-level names with a local import at the top of the function body, just after `import argparse`:

```python
    from strip_render import CONCENTRATED, RELAXED, focus_to_rgb
```

- [ ] **Step 6: Point the led_driver tests at the new home**

In `test_led_driver.py`, remove `focus_to_rgb` from the `led_driver` import list.
Do not re-import it from `strip_render` — once the ramp tests move out, nothing
left in this file uses it, and the remaining `FocusLight` tests assert on what
the fake strip was shown rather than on the ramp:

```python
from led_driver import (
    DNRGB_MAX_PIXELS,
    WLED_REALTIME_PORT,
    FocusLight,
    Smoother,
    WledStrip,
    build_dnrgb_packets,
    solid,
)
```

Then delete these tests from `test_led_driver.py` — they now live in `test_strip_render.py`:
`test_relaxed_is_blue`, `test_concentrated_is_red`, `test_midpoint_is_magenta`,
`test_neutral_drive_is_magenta_not_blue`, `test_fully_relaxed_is_blue`,
`test_relaxed_half_of_the_range_is_not_all_blue`.

Keep `test_ramp_never_dims_in_the_middle`, `test_ramp_has_no_green`,
`test_ramp_is_monotonic_in_red` and `test_out_of_range_scores_are_clamped` —
move them to `test_strip_render.py` unchanged.

- [ ] **Step 7: Run the full suite**

Run: `QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q`
Expected: PASS, 235 tests — 232 before this task, minus the 10 removed from
`test_led_driver.py` (6 deleted, 4 moved), plus the 13 now in
`test_strip_render.py`. If your number differs, reconcile it before continuing:
it means a test was lost in the move rather than relocated.

- [ ] **Step 8: Commit**

```bash
git add strip_render.py test_strip_render.py led_driver.py test_led_driver.py
git commit -m "refactor: split pixel rendering from strip transport"
```

---

### Task 6: Pixels in the snapshot and the simulated strip

**Files:**
- Modify: `led_driver.py` (`FocusLight.update` returns pixels)
- Modify: `server.py` (`_broadcast_loop` adds `lights` to the payload)
- Modify: `test_led_driver.py`, `test_server.py`
- Modify: `muse-pwa/src/serverApi.ts`, `Dashboard.tsx`, `Dashboard.css`

**Interfaces:**
- Consumes: `strip_render.render_solo` (Task 5), `WledStrip.show_pixels` (Task 2)
- Produces: `snapshot["lights"] = {"pixels": [r, g, b, ...]}` — a flat integer array, absent when no light is configured

- [ ] **Step 1: Write the failing test**

Add to `test_led_driver.py`:

```python
def test_focus_light_returns_the_pixels_it_painted():
    strip, clk = FakeStrip(), FakeClock(0.0)
    light = FocusLight(strip, seat="p1", tau_s=1.5, clock_fn=clk, count=3)
    pixels = light.update(_snapshot(normalized=1.0))
    assert pixels == [(255, 0, 0)] * 3


def test_focus_light_returns_none_when_released():
    strip, clk = FakeStrip(), FakeClock(0.0)
    light = FocusLight(strip, seat="p1", tau_s=1.5, clock_fn=clk, count=3)
    assert light.update(_snapshot(connected=False)) is None
```

Add to `test_server.py`:

```python
class TestStripInSnapshot:
    @async_test
    async def test_pixels_are_broadcast_to_observers(self, static_dir):
        hub = PlayerHub(seat_ids=make_seat_ids(2))
        strip = WledStrip("127.0.0.1", count=3, sock=_NullSocket())
        light = FocusLight(strip, seat="p1", count=3)
        app = create_app(static_dir=static_dir, hub=hub, light=light)

        async with TestClient(TestServer(app)) as client:
            async with client.ws_connect("/ws/observe") as observer:
                await observer.receive()
                async with client.ws_connect("/ws/p1") as player:
                    await player.send_json({"normalizedScore": 1.0})
                    for _ in range(20):
                        payload = json.loads((await observer.receive()).data)
                        if payload.get("lights", {}).get("pixels"):
                            assert payload["lights"]["pixels"] == [255, 0, 0] * 3
                            return
                    pytest.fail("observer never saw strip pixels")

    @async_test
    async def test_no_light_means_no_lights_key(self, static_dir):
        hub = PlayerHub(seat_ids=make_seat_ids(2))
        async with TestClient(TestServer(create_app(static_dir=static_dir, hub=hub))) as client:
            body = await (await client.get("/healthz")).json()
            assert "lights" not in body
```

Add this fake beside `FakeLight` in `test_server.py`:

```python
class _NullSocket:
    """Swallows datagrams so a test never touches the network."""

    def sendto(self, payload, addr):
        pass

    def close(self):
        pass
```

And extend the `server` import list in `test_server.py`:

```python
from led_driver import FocusLight, WledStrip
```

- [ ] **Step 2: Run test to verify it fails**

Run: `QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest test_led_driver.py test_server.py -k "returns_the_pixels or pixels_are_broadcast" -q`
Expected: FAIL — `TypeError: FocusLight.__init__() got an unexpected keyword argument 'count'`.

- [ ] **Step 3: Make `FocusLight` render pixels**

In `led_driver.py`, replace the `FocusLight` class:

```python
class FocusLight:
    """Turns hub snapshots into strip pixels for one watched seat."""

    def __init__(self, strip, seat: str = "p1", tau_s: float = 1.5,
                 clock_fn=None, count: int = 0):
        self._strip = strip
        self._seat = seat
        self._count = count
        self._smoother = Smoother(tau_s, clock_fn)

    def update(self, snapshot):
        """Drive one frame. Returns the pixels painted, or None if released."""
        from strip_render import render_solo

        score = self._score(snapshot)
        if score is None:
            self._smoother.reset()
            self._strip.release()
            return None
        pixels = render_solo(self._smoother.update(score), self._count)
        self._strip.show_pixels(pixels)
        return pixels
```

The four `FocusLight` tests that assert on `strip.shown` need their fake updated. In `test_led_driver.py`, replace `FakeStrip`:

```python
class FakeStrip:
    def __init__(self):
        self.shown = []
        self.released = 0

    def show_pixels(self, pixels):
        self.shown.append(list(pixels))

    def release(self):
        self.released += 1
```

and change the four colour assertions to compare pixel lists — each `FocusLight`
in those tests gains `count=1`, so `strip.shown == [[(255, 0, 0)]]`:

```python
def test_drives_the_strip_from_the_watched_seat():
    strip, clk = FakeStrip(), FakeClock(0.0)
    light = FocusLight(strip, seat="p1", tau_s=1.5, clock_fn=clk, count=1)
    light.update(_snapshot(normalized=1.0))
    assert strip.shown == [[(255, 0, 0)]]


def test_a_relaxed_wearer_turns_the_strip_blue():
    strip, clk = FakeStrip(), FakeClock(0.0)
    light = FocusLight(strip, seat="p1", tau_s=1.5, clock_fn=clk, count=1)
    light.update(_snapshot(normalized=-1.0))
    assert strip.shown == [[(0, 0, 255)]]


def test_other_seats_do_not_drive_the_strip():
    strip, clk = FakeStrip(), FakeClock(0.0)
    light = FocusLight(strip, seat="p1", tau_s=1.5, clock_fn=clk, count=1)
    snap = _snapshot(normalized=1.0)
    snap["seats"].append({
        "id": "p2", "raw": 0.0, "normalized": 0.0, "calibrating": False,
        "connected": True, "stale": False, "bands": None,
    })
    light.update(snap)
    assert strip.shown == [[(255, 0, 0)]]


def test_reconnecting_does_not_fade_from_the_pre_dropout_colour():
    strip, clk = FakeStrip(), FakeClock(0.0)
    light = FocusLight(strip, seat="p1", tau_s=1.5, clock_fn=clk, count=1)
    light.update(_snapshot(normalized=1.0))
    clk.t = 1.0
    light.update(_snapshot(connected=False))
    clk.t = 2.0
    light.update(_snapshot(normalized=-1.0))
    assert strip.shown[-1] == [(0, 0, 255)]


def test_smoothing_applies_across_successive_updates():
    strip, clk = FakeStrip(), FakeClock(0.0)
    light = FocusLight(strip, seat="p1", tau_s=1.5, clock_fn=clk, count=1)
    light.update(_snapshot(normalized=-1.0))
    clk.t = 0.1
    light.update(_snapshot(normalized=1.0))
    assert strip.shown[0] == [(0, 0, 255)]
    assert strip.shown[1] != [(255, 0, 0)]
```

Also add `"bands": None` to the `_snapshot` helper's seat dict in `test_led_driver.py`.

- [ ] **Step 4: Put the pixels in the broadcast payload**

In `server.py`, in `_broadcast_loop`, replace the light block:

```python
        if light is not None:
            try:
                pixels = light.update(snapshot)
            except Exception:
                # The invariant is worth more than the diagnosis: an unforeseen
                # failure in the lights must not end the task that flushes the
                # recording and feeds the dashboard. Debug level because this
                # would otherwise log ten times a second.
                logger.debug("Light update failed", exc_info=True)
            else:
                # Flat ints rather than base64: ~1.7 KB at 144 pixels is
                # nothing on a LAN, and it stays readable in devtools.
                snapshot["lights"] = {
                    "pixels": [c for pixel in (pixels or ()) for c in pixel]
                }
```

Pass the pixel count through in `main()`:

```python
        strip = WledStrip(args.led_host, count=args.led_count)
        light = FocusLight(strip, seat=args.led_seat, count=args.led_count)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q`
Expected: PASS, 239 tests (235 + 2 in `test_led_driver.py` + 2 in `test_server.py`).

- [ ] **Step 6: Add the dashboard types**

In `muse-pwa/src/serverApi.ts`, add:

```ts
export type LightsState = {
  pixels: number[];
};
```

and add to `HubState`:

```ts
  lights?: LightsState;
```

- [ ] **Step 7: Render the simulated strip**

In `muse-pwa/src/Dashboard.tsx`, add above the default export:

```tsx
/** Shows exactly the pixels the Pi is sending to WLED — the Pi renders, this
 *  only displays, so the simulation cannot drift from the real strip. */
function SimulatedStrip({ pixels }: { pixels: number[] }) {
  const count = Math.floor(pixels.length / 3);
  if (count === 0) return null;

  return (
    <section className="sim-strip-card glass-card">
      <h2 className="sim-strip-title">Strip</h2>
      <div className="sim-strip">
        {Array.from({ length: count }, (_, i) => (
          <span
            key={i}
            className="sim-px"
            style={{
              background: `rgb(${pixels[i * 3]}, ${pixels[i * 3 + 1]}, ${pixels[i * 3 + 2]})`,
            }}
          />
        ))}
      </div>
    </section>
  );
}
```

and render it just before the closing `</div>` of the dashboard, after the
`seats.length === 0` block:

```tsx
      <SimulatedStrip pixels={state.lights?.pixels ?? []} />
```

- [ ] **Step 8: Style it**

Append to `muse-pwa/src/Dashboard.css`:

```css
.sim-strip-card {
  margin-top: 1rem;
  padding: 0.75rem 1rem 1rem;
}

.sim-strip-title {
  margin: 0 0 0.5rem;
  font-size: 0.75rem;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--text-secondary);
}

.sim-strip {
  display: flex;
  gap: 1px;
  height: 28px;
  border-radius: 4px;
  overflow: hidden;
  background: #000;
}

.sim-px {
  flex: 1 1 0;
  min-width: 0;
}
```

- [ ] **Step 9: Build the PWA**

Run: `cd muse-pwa && npm run build && cd ..`
Expected: build succeeds with no type errors.

- [ ] **Step 10: Verify against a running server**

Run in one shell:

```bash
.venv/bin/python server.py --port 8443 --led-host 127.0.0.1 --led-count 60
```

Open `https://localhost:8443/dashboard`, pair a headset (or use the mock data
generator), and confirm the strip at the bottom shows a colour that tracks the
seat's drive: blue when relaxed, magenta at baseline, red when focused. Confirm
it goes empty when the seat disconnects.

- [ ] **Step 11: Update the README**

In `README.md`, add to the end of the Lights section:

```markdown
The dashboard shows a simulated strip along the bottom. The Pi renders the
pixels and sends that same array to both WLED and the dashboard, so what you
see there is what the strip is doing — there is no second renderer that could
disagree.
```

Update the test count in the Development section to `239 tests`.

- [ ] **Step 12: Commit**

```bash
git add led_driver.py server.py test_led_driver.py test_server.py README.md muse-pwa/src
git commit -m "feat: render strip pixels on the Pi and mirror them on the dashboard"
```

---

## Done when

- `QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q` reports 239 passing
- `cd muse-pwa && npm run build` succeeds
- The dashboard shows a simulated strip that tracks a seat's drive across the full `[-1, +1]` range
- Live band powers are visible in the snapshot for any connected seat, whether or not a session is recording

## Next plan

`docs/superpowers/plans/2026-08-15-battle-mode-1.md` covers spec steps 7–10:
`battle.py`, the game tick and config endpoints, host controls, and calibration
on connect with host overrides.
