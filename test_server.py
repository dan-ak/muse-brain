import asyncio
import csv
import functools
import json
import pathlib
import ssl
import subprocess

import pytest
from aiohttp.test_utils import TestClient, TestServer

from led_driver import FocusLight, WledStrip
from server import (
    CALIBRATED,
    IGNORED,
    TELEMETRY,
    PlayerHub,
    build_ssl_context,
    create_app,
    make_redirect_app,
    make_seat_ids,
    parse_args,
)
from session_recorder import SessionRecorder


def async_test(fn):
    """Run an async test body without pulling in an asyncio pytest plugin."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))

    return wrapper


async def _settle(seconds=0.05):
    """Yield long enough for the server side of a socket to process a frame."""
    await asyncio.sleep(seconds)


def _read_csv(path):
    with open(path, newline="") as f:
        return list(csv.reader(f))


@pytest.fixture
def static_dir(tmp_path):
    """A stand-in for muse-pwa/dist."""
    root = tmp_path / "dist"
    root.mkdir()
    (root / "index.html").write_text("<!doctype html><title>Muse</title>")
    (root / "sw.js").write_text("// service worker")
    assets = root / "assets"
    assets.mkdir()
    (assets / "app.js").write_text("export const x = 1;")
    return root


@pytest.fixture
def recorder(tmp_path):
    return SessionRecorder(base_dir=tmp_path / "recordings")


@pytest.fixture
def self_signed(tmp_path):
    """A throwaway certificate so TLS is testable without Let's Encrypt."""
    cert = tmp_path / "cert.pem"
    key = tmp_path / "key.pem"
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-keyout", str(key), "-out", str(cert),
            "-days", "1", "-subj", "/CN=muse.test",
        ],
        check=True,
        capture_output=True,
    )
    return cert, key


class TestSeatRoster:
    def test_seats_are_named_p1_upwards(self):
        assert make_seat_ids(3) == ("p1", "p2", "p3")

    def test_a_roster_needs_at_least_one_seat(self):
        with pytest.raises(ValueError):
            make_seat_ids(0)

    def test_hub_accepts_an_explicit_roster(self):
        hub = PlayerHub(seat_ids=make_seat_ids(3))
        assert hub.is_known("p3")
        # The cap is the roster, not a hardcoded pair.
        assert not hub.is_known("p4")
        assert not hub.is_known("../etc/passwd")

    @async_test
    async def test_roster_is_published_for_the_client(self, static_dir):
        app = create_app(static_dir=static_dir, hub=PlayerHub(seat_ids=make_seat_ids(3)))
        async with TestClient(TestServer(app)) as client:
            response = await client.get("/api/seats")
            assert await response.json() == {"seats": ["p1", "p2", "p3"]}


class TestPlayerHub:
    def test_telemetry_updates_state(self):
        hub = PlayerHub()
        outcome = hub.handle_message(
            "p1",
            json.dumps({"rawScore": 1.5, "normalizedScore": 0.25, "isCalibrating": True}),
        )
        assert outcome == TELEMETRY
        assert hub.state_of("p1") == {
            "raw": 1.5, "normalized": 0.25, "calibrating": True, "bands": None,
        }

    def test_calibration_event_does_not_clobber_scores(self):
        hub = PlayerHub()
        hub.handle_message("p1", json.dumps({"rawScore": 2.0, "normalizedScore": 1.0}))
        outcome = hub.handle_message(
            "p1",
            json.dumps({"event": "calibration_complete", "baseline": 0.4, "halfRange": 0.9}),
        )
        assert outcome == CALIBRATED
        assert hub.state_of("p1")["raw"] == 2.0

    def test_calibration_event_missing_fields_is_still_calibration(self):
        hub = PlayerHub()
        assert hub.handle_message("p1", json.dumps({"event": "calibration_complete"})) == CALIBRATED

    @pytest.mark.parametrize(
        "payload",
        [
            "{not json",
            "",
            json.dumps([1, 2, 3]),
            json.dumps("a string"),
            json.dumps({"rawScore": "not a number"}),
            json.dumps({"normalizedScore": None}),
        ],
    )
    def test_malformed_frames_are_ignored(self, payload):
        hub = PlayerHub()
        assert hub.handle_message("p1", payload) == IGNORED
        assert hub.state_of("p1") == {
            "raw": 0.0, "normalized": 0.0, "calibrating": False, "bands": None,
        }

    def test_attach_reports_the_socket_it_displaced(self):
        hub = PlayerHub()
        first, second = object(), object()
        assert hub.attach("p1", first) is None
        assert hub.attach("p1", second) is first

    def test_stale_socket_cannot_evict_its_replacement(self):
        hub = PlayerHub()
        stale, live = object(), object()
        hub.attach("p1", stale)
        hub.attach("p1", live)

        # The stale handler unwinds after the reconnect has already registered.
        assert hub.detach("p1", stale) is False
        assert hub.occupied_seats == ("p1",)

        assert hub.detach("p1", live) is True
        assert hub.occupied_seats == ()

    def test_snapshot_covers_every_seat_and_marks_occupancy(self):
        hub = PlayerHub(seat_ids=make_seat_ids(3))
        hub.attach("p2", object())
        snapshot = hub.snapshot()

        assert [seat["id"] for seat in snapshot["seats"]] == ["p1", "p2", "p3"]
        occupancy = {seat["id"]: seat["connected"] for seat in snapshot["seats"]}
        assert occupancy == {"p1": False, "p2": True, "p3": False}
        assert snapshot["recording"]["active"] is False

    def test_seat_goes_stale_when_telemetry_stops(self):
        # An open socket is not evidence of a live headset: a slept phone or a
        # dropped Muse leaves the websocket up while the data stops.
        now = [1000.0]
        hub = PlayerHub(seat_ids=make_seat_ids(2), clock=lambda: now[0])
        hub.attach("p1", object())
        hub.handle_message("p1", json.dumps({"normalizedScore": 0.5}))
        assert hub.is_stale("p1") is False

        now[0] += 1.0
        assert hub.is_stale("p1") is False, "still inside the grace window"

        now[0] += 5.0
        assert hub.is_stale("p1") is True

        # Fresh telemetry revives it without needing a reconnect.
        hub.handle_message("p1", json.dumps({"normalizedScore": 0.6}))
        assert hub.is_stale("p1") is False

    def test_a_seat_repeating_one_value_goes_stale(self):
        # Observed live: a tab whose headset had dropped kept streaming its last
        # score at 30 Hz. Frames were arriving, so a receive-based check called
        # it live while the dashboard showed a number frozen for minutes.
        now = [0.0]
        hub = PlayerHub(clock=lambda: now[0])
        hub.attach("p1", object())
        frozen = json.dumps({"rawScore": 1.0, "normalizedScore": -0.1294941623381023})

        hub.handle_message("p1", frozen)
        assert hub.is_stale("p1") is False

        for _ in range(200):
            now[0] += 0.05
            hub.handle_message("p1", frozen)

        assert hub.is_stale("p1") is True, "identical readings are not liveness"

        # A genuinely new reading revives it.
        hub.handle_message("p1", json.dumps({"rawScore": 1.0, "normalizedScore": -0.13}))
        assert hub.is_stale("p1") is False

    def test_a_seat_that_never_sends_goes_stale(self):
        now = [0.0]
        hub = PlayerHub(clock=lambda: now[0])
        hub.attach("p1", object())
        assert hub.is_stale("p1") is False
        now[0] += 10.0
        assert hub.is_stale("p1") is True

    def test_an_empty_seat_is_never_stale(self):
        # Empty already says everything; stale would be noise on top of it.
        now = [0.0]
        hub = PlayerHub(clock=lambda: now[0])
        now[0] += 10_000.0
        assert hub.is_stale("p1") is False

    def test_disconnect_clears_the_staleness_clock(self):
        now = [0.0]
        hub = PlayerHub(clock=lambda: now[0])
        socket = object()
        hub.attach("p1", socket)
        now[0] += 10.0
        assert hub.is_stale("p1") is True
        hub.detach("p1", socket)
        assert hub.is_stale("p1") is False

    def test_rate_reflects_the_arrival_interval(self):
        now = [0.0]
        hub = PlayerHub(clock=lambda: now[0])
        hub.attach("p1", object())
        for i in range(60):
            now[0] += 1 / 30
            hub.handle_message("p1", json.dumps({"normalizedScore": i / 100}))
        assert 25 <= hub.rate_of("p1") <= 35
        assert hub.is_throttled("p1") is False

    def test_a_backgrounded_tab_reads_as_throttled(self):
        # Browsers clamp timers in hidden tabs to ~1 Hz, so a pocketed phone
        # keeps streaming and keeps looking connected while its data rate
        # collapses. Observed live: 59 rows in 58s against another seat's 1422.
        now = [0.0]
        hub = PlayerHub(clock=lambda: now[0])
        hub.attach("p1", object())
        for i in range(10):
            now[0] += 1.0
            hub.handle_message("p1", json.dumps({"normalizedScore": i / 100}))

        assert hub.rate_of("p1") < 2.0
        assert hub.is_throttled("p1") is True
        # Throttled is not stale: the data is still arriving and still changing.
        assert hub.is_stale("p1") is False

    def test_an_empty_or_stale_seat_is_not_reported_throttled(self):
        # Throttled means "connected but slow"; it would be noise on a seat
        # that is empty or already flagged as silent.
        now = [0.0]
        hub = PlayerHub(clock=lambda: now[0])
        assert hub.is_throttled("p1") is False

        hub.attach("p1", object())
        hub.handle_message("p1", json.dumps({"normalizedScore": 0.5}))
        now[0] += 30.0
        assert hub.is_stale("p1") is True
        assert hub.is_throttled("p1") is False

    def test_snapshot_carries_rate_and_throttle_flags(self):
        now = [0.0]
        hub = PlayerHub(seat_ids=make_seat_ids(2), clock=lambda: now[0])
        hub.attach("p1", object())
        for i in range(60):
            now[0] += 1 / 30
            hub.handle_message("p1", json.dumps({"normalizedScore": i / 100}))

        seats = {s["id"]: s for s in hub.snapshot()["seats"]}
        assert 25 <= seats["p1"]["rate"] <= 35
        assert seats["p1"]["throttled"] is False
        assert seats["p2"]["rate"] == 0.0
        assert seats["p2"]["throttled"] is False

    def test_snapshot_reports_staleness_per_seat(self):
        now = [0.0]
        hub = PlayerHub(seat_ids=make_seat_ids(2), clock=lambda: now[0])
        hub.attach("p1", object())
        hub.handle_message("p1", json.dumps({"normalizedScore": 0.5}))
        now[0] += 10.0
        hub.attach("p2", object())
        hub.handle_message("p2", json.dumps({"normalizedScore": -0.5}))

        seats = {s["id"]: s for s in hub.snapshot()["seats"]}
        assert seats["p1"] == {
            "id": "p1", "connected": True, "stale": True,
            "raw": 0.0, "normalized": 0.5, "calibrating": False, "bands": None,
            # A single arrival gives no interval to measure, and a stale seat
            # is never also reported as throttled.
            "rate": 0.0, "throttled": False,
            # No raw batches have arrived, so nothing has been lost.
            "overflows": 0,
        }
        assert seats["p2"]["stale"] is False

    def test_snapshot_does_not_alias_internal_state(self):
        hub = PlayerHub()
        snapshot = hub.snapshot()
        snapshot["seats"][0]["raw"] = 99.0
        assert hub.state_of("p1")["raw"] == 0.0


def _raw_batch(**streams):
    return json.dumps(dict(type="raw", **streams))


class TestRawCapture:
    def test_eeg_samples_are_written_one_row_per_sample(self, recorder):
        hub = PlayerHub(recorder=recorder)
        session = hub.start_recording("raw")
        hub.handle_message(
            "p1",
            _raw_batch(eeg=[[1.0, 2.0, 3.0, 4.0], [5.0, 6.0, 7.0, 8.0]]),
        )
        hub.stop_recording()

        rows = _read_csv(session / "eeg.csv")
        assert rows[0] == ["t", "seat", "TP9", "AF7", "AF8", "TP10"]
        assert [r[1:] for r in rows[1:]] == [
            ["p1", "1.0", "2.0", "3.0", "4.0"],
            ["p1", "5.0", "6.0", "7.0", "8.0"],
        ]

    def test_samples_in_a_batch_are_spaced_by_the_sample_rate(self, recorder):
        # A batch carries no per-sample timestamps, so they are back-dated from
        # arrival. Spacing within the batch must be exact or the signal is
        # time-warped.
        hub = PlayerHub(recorder=recorder)
        session = hub.start_recording("spacing")
        hub.handle_message("p1", _raw_batch(eeg=[[i, 0, 0, 0] for i in range(4)]))
        hub.stop_recording()

        times = [float(r[0]) for r in _read_csv(session / "eeg.csv")[1:]]
        gaps = [b - a for a, b in zip(times, times[1:])]
        # Timestamps are stored to 6 decimals, so a 1 us quantisation against a
        # 3906 us sample interval is expected and harmless.
        assert gaps == pytest.approx([1 / 256] * 3, abs=2e-6)
        assert times == sorted(times), "samples must be in chronological order"

    def test_every_stream_lands_in_its_own_file(self, recorder):
        hub = PlayerHub(recorder=recorder)
        session = hub.start_recording("streams")
        hub.handle_message(
            "p2",
            _raw_batch(
                eeg=[[1, 2, 3, 4]],
                ppg=[[10, 11, 12]],
                acc=[[0.1, 0.2, 0.3]],
                gyro=[[1.5, 2.5, 3.5]],
                bands=[[0, 1, 2, 3, 4, 5]],
            ),
        )
        hub.stop_recording()

        assert _read_csv(session / "ppg.csv")[0] == ["t", "seat", "ppg1", "ppg2", "ppg3"]
        assert _read_csv(session / "acc.csv")[1][1:] == ["p2", "0.1", "0.2", "0.3"]
        assert _read_csv(session / "gyro.csv")[1][1:] == ["p2", "1.5", "2.5", "3.5"]
        bands = _read_csv(session / "bands.csv")
        assert bands[0] == ["t", "seat", "channel", "delta", "theta", "alpha", "beta", "gamma"]
        assert bands[1][1:] == ["p2", "0", "1", "2", "3", "4", "5"]

    def test_a_headset_without_ppg_is_not_an_error(self, recorder):
        # The MU-02 has no PPG hardware, so its batches simply omit the stream.
        hub = PlayerHub(recorder=recorder)
        session = hub.start_recording("mu02")
        hub.handle_message("p1", _raw_batch(eeg=[[1, 2, 3, 4]], acc=[[0, 0, 0]]))
        hub.stop_recording()

        assert (session / "eeg.csv").exists()
        assert not (session / "ppg.csv").exists()
        meta = json.loads((session / "meta.json").read_text())
        assert meta["errors"] == 0

    @pytest.mark.parametrize(
        "batch",
        [
            _raw_batch(eeg="not a list"),
            _raw_batch(eeg=[[1, 2]]),           # wrong width
            _raw_batch(eeg=[]),                  # empty
            _raw_batch(unknown_stream=[[1, 2]]),  # a newer client
            _raw_batch(bands=[[1, 2]]),          # wrong band width
        ],
    )
    def test_malformed_batches_are_ignored_without_erroring(self, recorder, batch):
        hub = PlayerHub(recorder=recorder)
        session = hub.start_recording("malformed")
        hub.handle_message("p1", batch)
        hub.stop_recording()

        meta = json.loads((session / "meta.json").read_text())
        assert meta["errors"] == 0
        assert "eeg.csv" not in meta["streams"] or meta["streams"]["eeg.csv"] == 0

    def test_raw_is_discarded_when_no_session_is_recording(self, recorder):
        # Clients should not be streaming, but a batch in flight as a session
        # stops must not become an error or a stray file.
        hub = PlayerHub(recorder=recorder)
        hub.handle_message("p1", _raw_batch(eeg=[[1, 2, 3, 4]]))
        assert hub.recording is False
        assert not recorder.base_dir.exists() or not list(recorder.base_dir.iterdir())

    def test_overflow_counts_accumulate_and_reach_the_snapshot(self, recorder):
        hub = PlayerHub(recorder=recorder)
        hub.start_recording("drops")
        hub.handle_message("p1", _raw_batch(eeg=[[1, 2, 3, 4]], overflows={"eeg": 12}))
        hub.handle_message("p1", _raw_batch(eeg=[[1, 2, 3, 4]], overflows={"eeg": 5, "ppg": 2}))
        hub.stop_recording()

        assert hub.overflows_for("p1") == 19
        seats = {s["id"]: s for s in hub.snapshot()["seats"]}
        assert seats["p1"]["overflows"] == 19
        assert seats["p2"]["overflows"] == 0

    def test_overflow_counts_reset_between_sessions(self, recorder):
        hub = PlayerHub(recorder=recorder)
        hub.start_recording("first")
        hub.handle_message("p1", _raw_batch(eeg=[[1, 2, 3, 4]], overflows={"eeg": 7}))
        hub.stop_recording()
        assert hub.overflows_for("p1") == 7

        hub.start_recording("second")
        assert hub.overflows_for("p1") == 0, "drops describe one session"
        hub.stop_recording()

    def test_nonsense_overflow_values_are_ignored(self, recorder):
        hub = PlayerHub(recorder=recorder)
        hub.start_recording("bad-drops")
        hub.handle_message("p1", _raw_batch(eeg=[[1, 2, 3, 4]], overflows="lots"))
        hub.handle_message("p1", _raw_batch(eeg=[[1, 2, 3, 4]], overflows={"eeg": "many"}))
        hub.stop_recording()
        assert hub.overflows_for("p1") == 0

    def test_raw_does_not_disturb_the_focus_score(self, recorder):
        hub = PlayerHub(recorder=recorder)
        hub.start_recording("mixed")
        hub.handle_message("p1", json.dumps({"rawScore": 2.0, "normalizedScore": 0.5}))
        hub.handle_message("p1", _raw_batch(eeg=[[1, 2, 3, 4]]))
        hub.stop_recording()

        assert hub.state_of("p1") == {
            "raw": 2.0, "normalized": 0.5, "calibrating": False, "bands": None,
        }

    def test_recording_message_reflects_session_state(self, recorder):
        hub = PlayerHub(recorder=recorder)
        assert hub.recording_message() == {"type": "recording", "active": False, "label": ""}
        hub.start_recording("live-one")
        assert hub.recording_message() == {
            "type": "recording", "active": True, "label": "live-one",
        }
        hub.stop_recording()
        assert hub.recording_message()["active"] is False

    def test_flush_is_safe_with_no_session(self, recorder):
        hub = PlayerHub(recorder=recorder)
        hub.flush_recorder()  # must not raise
        hub.start_recording("flushing")
        hub.handle_message("p1", _raw_batch(eeg=[[1, 2, 3, 4]]))
        hub.flush_recorder()
        assert hub.recording is True
        hub.stop_recording()


class TestRawCaptureHardening:
    """Regressions for defects found by review after the first implementation."""

    def test_infinite_overflow_count_does_not_raise(self, recorder):
        # json.loads accepts a bare Infinity literal, and int() raises
        # OverflowError on it — which is not a ValueError, so it escaped
        # handle_message and tore the player's socket down.
        hub = PlayerHub(recorder=recorder)
        hub.start_recording("inf")
        outcome = hub.handle_message(
            "p1", '{"type":"raw","eeg":[[1,2,3,4]],"overflows":{"eeg":Infinity}}'
        )
        assert outcome is not None
        assert hub.overflows_for("p1") == 0
        hub.stop_recording()

    @pytest.mark.parametrize("literal", ["Infinity", "-Infinity", "NaN"])
    def test_non_finite_overflow_counts_are_ignored(self, recorder, literal):
        hub = PlayerHub(recorder=recorder)
        hub.start_recording("nonfinite")
        hub.handle_message("p1", '{"type":"raw","overflows":{"eeg":%s}}' % literal)
        assert hub.overflows_for("p1") == 0
        hub.stop_recording()

    def test_timestamps_never_go_backwards_across_batches(self, recorder):
        # A batch delayed by wifi power-save used to be back-dated to before rows
        # already on disk, leaving a non-monotonic time column.
        now = [0.0]
        hub = PlayerHub(recorder=recorder, clock=lambda: now[0])
        session = hub.start_recording("monotonic")

        for _ in range(6):
            now[0] += 0.05
            hub.handle_message(
                "p1", _raw_batch(eeg=[[i, 0, 0, 0] for i in range(32)])
            )
        hub.stop_recording()

        times = [float(r[0]) for r in _read_csv(session / "eeg.csv")[1:]]
        assert times == sorted(times), "eeg.csv must be chronological"
        assert len(set(times)) == len(times), "no duplicate timestamps"

    @pytest.mark.parametrize(
        "batch",
        [
            '{"type":"raw","eeg":[[null,null,null,null]]}',
            '{"type":"raw","eeg":[["a","b","c","d"]]}',
            '{"type":"raw","eeg":[[1,2,3,{"k":1}]]}',
            '{"type":"raw","eeg":[[1,2,3,[4]]]}',
            '{"type":"raw","eeg":[[NaN,1,2,3]]}',
            '{"type":"raw","eeg":[[Infinity,1,2,3]]}',
            '{"type":"raw","eeg":[[true,false,true,false]]}',
            '{"type":"raw","bands":[[0,null,"x",{"k":1},1,2]]}',
        ],
    )
    def test_non_numeric_samples_never_reach_a_csv(self, recorder, batch):
        # Length was checked but not type, so None/strings/dicts were written
        # verbatim into numeric columns with errors still reported as 0.
        hub = PlayerHub(recorder=recorder)
        session = hub.start_recording("junk")
        hub.handle_message("p1", batch)
        hub.stop_recording()

        for name in ("eeg.csv", "bands.csv"):
            path = session / name
            if not path.exists():
                continue
            for row in _read_csv(path)[1:]:
                for cell in row[2:]:
                    assert cell not in ("", "None", "nan", "inf", "-inf", "True", "False")
                    float(cell)  # must parse as a number

    def test_valid_samples_still_get_through(self, recorder):
        hub = PlayerHub(recorder=recorder)
        session = hub.start_recording("valid")
        hub.handle_message("p1", _raw_batch(eeg=[[1.5, -2.5, 0.0, 3]]))
        hub.stop_recording()
        assert _read_csv(session / "eeg.csv")[1][2:] == ["1.5", "-2.5", "0.0", "3"]

    def test_mixed_batch_keeps_good_rows_and_drops_bad(self, recorder):
        hub = PlayerHub(recorder=recorder)
        session = hub.start_recording("mixed")
        hub.handle_message(
            "p1", '{"type":"raw","eeg":[[1,2,3,4],[null,null,null,null],[5,6,7,8]]}'
        )
        hub.stop_recording()
        rows = [r[2:] for r in _read_csv(session / "eeg.csv")[1:]]
        assert rows == [["1", "2", "3", "4"], ["5", "6", "7", "8"]]

    def test_band_ticks_get_distinct_timestamps(self, recorder):
        # Rows arrive four at a time per DSP tick. Stamping a delayed batch's
        # several ticks with one arrival collapsed distinct windows, so anything
        # pivoting on (t, channel) silently discarded all but one.
        hub = PlayerHub(recorder=recorder)
        session = hub.start_recording("ticks")
        two_ticks = [[ch, 1, 2, 3, 4, 5] for ch in range(4)] * 2
        hub.handle_message("p1", _raw_batch(bands=two_ticks))
        hub.stop_recording()

        rows = _read_csv(session / "bands.csv")[1:]
        assert len(rows) == 8
        pairs = {(r[0], r[2]) for r in rows}
        assert len(pairs) == 8, "each (t, channel) must be unique"
        assert len({r[0] for r in rows}) == 2, "two ticks means two timestamps"

    def test_reported_loss_is_written_to_the_recording(self, recorder):
        # The overflow count lived only in memory, so a dropout left no trace on
        # disk and the rows looked continuous.
        hub = PlayerHub(recorder=recorder)
        session = hub.start_recording("gaps")
        hub.handle_message("p1", _raw_batch(eeg=[[1, 2, 3, 4]]))
        hub.handle_message("p1", _raw_batch(eeg=[[5, 6, 7, 8]], overflows={"eeg": 40}))
        hub.stop_recording()

        gaps = _read_csv(session / "gaps.csv")
        assert gaps[0] == ["t", "seat", "stream", "resumed_at"]
        assert gaps[1][1:3] == ["p1", "/pwa/eeg"]

    def test_timestamps_are_never_negative(self, recorder):
        # The first batch covers samples buffered before the session started, so
        # back-dating pushed them before t=0. A real session produced 7 such
        # rows; no downstream tool expects a negative time column.
        hub = PlayerHub(recorder=recorder)
        session = hub.start_recording("t-zero")
        hub.handle_message("p1", _raw_batch(eeg=[[i, 0, 0, 0] for i in range(64)]))
        hub.stop_recording()

        times = [float(r[0]) for r in _read_csv(session / "eeg.csv")[1:]]
        assert min(times) >= 0.0, "no sample may predate the session"
        assert times == sorted(times)

    def test_no_gap_row_without_reported_loss(self, recorder):
        hub = PlayerHub(recorder=recorder)
        session = hub.start_recording("nogaps")
        hub.handle_message("p1", _raw_batch(eeg=[[1, 2, 3, 4]]))
        hub.stop_recording()
        assert not (session / "gaps.csv").exists()

    def test_starting_an_active_session_again_changes_nothing(self, recorder):
        # SessionRecorder.start is a no-op while active and keeps its directory,
        # but the hub used to overwrite the label, zero the loss counts and
        # re-emit connect rows anyway.
        hub = PlayerHub(recorder=recorder)
        first = hub.start_recording("original")
        hub.attach("p1", object())
        hub.handle_message("p1", _raw_batch(eeg=[[1, 2, 3, 4]], overflows={"eeg": 9}))
        assert hub.overflows_for("p1") == 9

        second = hub.start_recording("corrected")
        assert second == first, "the directory must not change"
        assert hub.recording_message()["label"] == "original"
        assert hub.overflows_for("p1") == 9, "counts describe the live session"

        hub.stop_recording()
        connects = [r for r in _read_csv(first / "seats.csv")[1:] if r[2] == "connect"]
        assert len(connects) == 1, "no phantom reconnect rows"


def _cue(**kw):
    return json.dumps(dict(type="cue", **kw))


class TestCueMarkers:
    """Without these a recording cannot be evaluated even by its own author."""

    def test_a_trial_cue_is_recorded_with_its_condition(self, recorder):
        hub = PlayerHub(recorder=recorder)
        session = hub.start_recording("cued")
        hub.handle_message("p1", _cue(phase="trial", eyes="closed", task="focus", trial=3))
        hub.stop_recording()

        rows = _read_csv(session / "cues.csv")
        assert rows[0] == ["t", "seat", "phase", "eyes", "task", "trial"]
        assert rows[1][1:] == ["p1", "trial", "closed", "focus", "3"]

    def test_rest_and_done_carry_no_condition(self, recorder):
        hub = PlayerHub(recorder=recorder)
        session = hub.start_recording("rests")
        hub.handle_message("p1", _cue(phase="rest"))
        hub.handle_message("p1", _cue(phase="done"))
        hub.stop_recording()

        rows = _read_csv(session / "cues.csv")[1:]
        assert [r[2] for r in rows] == ["rest", "done"]
        assert all(r[3] == "" and r[4] == "" for r in rows)

    def test_the_full_2x2_round_trips(self, recorder):
        hub = PlayerHub(recorder=recorder)
        session = hub.start_recording("factorial")
        for i, (eyes, task) in enumerate(
            [("open", "calm"), ("open", "focus"), ("closed", "calm"), ("closed", "focus")], 1
        ):
            hub.handle_message("p1", _cue(phase="trial", eyes=eyes, task=task, trial=i))
        hub.stop_recording()

        rows = _read_csv(session / "cues.csv")[1:]
        assert [(r[3], r[4]) for r in rows] == [
            ("open", "calm"), ("open", "focus"), ("closed", "calm"), ("closed", "focus"),
        ]

    @pytest.mark.parametrize(
        "payload",
        [
            _cue(phase="not-a-phase"),
            _cue(),
            json.dumps({"type": "cue", "phase": 7}),
        ],
    )
    def test_unknown_phases_are_ignored(self, recorder, payload):
        hub = PlayerHub(recorder=recorder)
        session = hub.start_recording("badcue")
        assert hub.handle_message("p1", payload) == IGNORED
        hub.stop_recording()
        assert not (session / "cues.csv").exists()

    def test_a_nonsense_trial_number_does_not_raise(self, recorder):
        hub = PlayerHub(recorder=recorder)
        session = hub.start_recording("badtrial")
        hub.handle_message("p1", _cue(phase="trial", eyes="open", task="calm", trial="many"))
        hub.stop_recording()
        assert _read_csv(session / "cues.csv")[1][5] == ""

    def test_cues_do_not_disturb_telemetry(self, recorder):
        hub = PlayerHub(recorder=recorder)
        hub.start_recording("mixed")
        hub.handle_message("p1", json.dumps({"rawScore": 2.0, "normalizedScore": 0.5}))
        hub.handle_message("p1", _cue(phase="trial", eyes="open", task="focus", trial=1))
        hub.stop_recording()
        assert hub.state_of("p1") == {
            "raw": 2.0, "normalized": 0.5, "calibrating": False, "bands": None,
        }

    def test_cues_outside_a_session_are_harmless(self, recorder):
        hub = PlayerHub(recorder=recorder)
        assert hub.handle_message("p1", _cue(phase="trial", eyes="open", task="calm")) is not None
        assert hub.recording is False


class TestAutoFlush:
    def test_rows_reach_disk_without_anyone_calling_flush(self, tmp_path):
        # The Qt visualizer and neurofeedback view never call flush(), so
        # buffering must not depend on callers remembering to.
        now = [0.0]
        rec = SessionRecorder(base_dir=tmp_path / "rec", clock_fn=lambda: now[0])
        session = rec.start("autoflush")
        rec.record("/pwa/seat", ["p1", "connect"])

        now[0] += SessionRecorder.AUTO_FLUSH_S + 0.1
        rec.record("/pwa/seat", ["p1", "disconnect"])

        # Read without stopping: a crash would look exactly like this.
        rows = _read_csv(session / "seats.csv")
        assert len(rows) >= 2, "buffered rows must have been flushed"
        rec.stop()

    def test_flush_is_idempotent_and_safe_when_inactive(self, tmp_path):
        rec = SessionRecorder(base_dir=tmp_path / "rec")
        rec.flush()  # no session at all
        rec.start("s")
        rec.flush()
        rec.flush()
        rec.stop()
        rec.flush()  # after stop


class TestRecording:
    def test_nothing_is_written_until_a_session_starts(self, recorder):
        hub = PlayerHub(recorder=recorder)
        hub.handle_message("p1", json.dumps({"rawScore": 1.0, "normalizedScore": 0.5}))
        assert hub.recording is False
        assert not (recorder.base_dir).exists() or not list(recorder.base_dir.iterdir())

    def test_telemetry_is_recorded_with_its_seat(self, recorder):
        hub = PlayerHub(recorder=recorder)
        session = hub.start_recording("three-up")

        hub.handle_message("p1", json.dumps({"rawScore": 1.0, "normalizedScore": 0.5}))
        hub.handle_message("p3", json.dumps({"rawScore": -2.0, "normalizedScore": -0.25}))
        hub.stop_recording()

        rows = _read_csv(session / "telemetry.csv")
        assert rows[0] == ["t", "seat", "focus_score", "normalized", "calibrating"]
        assert [r[1] for r in rows[1:]] == ["p1", "p3"]
        assert rows[1][2:] == ["1.0", "0.5", "0"]

    def test_calibration_results_are_recorded(self, recorder):
        # A normalized score is uninterpretable later without these.
        hub = PlayerHub(recorder=recorder)
        session = hub.start_recording("cal")
        hub.handle_message(
            "p2",
            json.dumps({"event": "calibration_complete", "baseline": 0.4, "halfRange": 0.9}),
        )
        hub.stop_recording()

        rows = _read_csv(session / "calibration.csv")
        assert rows[0] == ["t", "seat", "baseline", "half_range"]
        assert rows[1][1:] == ["p2", "0.4", "0.9"]

    def test_seat_transitions_are_recorded(self, recorder):
        # A gap in telemetry is otherwise ambiguous: a headset that fell off
        # looks the same as a phone that locked itself.
        hub = PlayerHub(recorder=recorder)
        session = hub.start_recording("transitions")
        socket = object()
        hub.attach("p1", socket)
        hub.detach("p1", socket)
        hub.stop_recording()

        rows = _read_csv(session / "seats.csv")
        assert rows[1][1:] == ["p1", "connect"]
        assert rows[2][1:] == ["p1", "disconnect"]

    def test_seats_already_occupied_are_noted_at_session_start(self, recorder):
        hub = PlayerHub(recorder=recorder)
        hub.attach("p1", object())
        session = hub.start_recording("late-start")
        hub.stop_recording()

        rows = _read_csv(session / "seats.csv")
        assert rows[1][1:] == ["p1", "connect"]

    def test_meta_records_the_session_label(self, recorder):
        hub = PlayerHub(recorder=recorder)
        session = hub.start_recording("burn-night")
        hub.stop_recording()

        meta = json.loads((session / "meta.json").read_text())
        assert meta["label"] == "burn-night"
        assert meta["status"] == "complete"

    def test_a_broken_recorder_does_not_break_telemetry(self):
        class Exploding:
            active = True

            def record(self, *args, **kwargs):
                raise RuntimeError("disk gone")

        hub = PlayerHub(recorder=Exploding())
        # Delivering telemetry matters more than recording it.
        assert hub.handle_message("p1", json.dumps({"normalizedScore": 0.5})) == TELEMETRY
        assert hub.state_of("p1")["normalized"] == 0.5

    def test_hub_without_a_recorder_still_works(self):
        hub = PlayerHub()
        assert hub.start_recording("noop") is None
        assert hub.stop_recording() is None
        assert hub.recording is False
        assert hub.handle_message("p1", json.dumps({"normalizedScore": 1.0})) == TELEMETRY


class TestSSLContext:
    def test_no_certificate_means_plain_http(self):
        assert build_ssl_context(None, None) is None
        assert build_ssl_context("", "") is None

    def test_certificate_without_key_is_an_error(self, self_signed):
        cert, _ = self_signed
        with pytest.raises(ValueError, match="half configured"):
            build_ssl_context(str(cert), None)

    def test_key_without_certificate_is_an_error(self, self_signed):
        _, key = self_signed
        with pytest.raises(ValueError, match="half configured"):
            build_ssl_context(None, str(key))

    def test_missing_certificate_file_is_an_error(self, tmp_path):
        with pytest.raises(FileNotFoundError, match="certificate"):
            build_ssl_context(str(tmp_path / "nope.pem"), str(tmp_path / "nope.key"))

    def test_valid_pair_builds_a_context(self, self_signed):
        cert, key = self_signed
        assert isinstance(build_ssl_context(str(cert), str(key)), ssl.SSLContext)


class TestApp:
    @async_test
    async def test_health_reports_the_whole_hub(self, static_dir):
        async with TestClient(TestServer(create_app(static_dir=static_dir))) as client:
            body = await (await client.get("/healthz")).json()
            assert body["type"] == "state"
            assert len(body["seats"]) == 4

    @async_test
    async def test_root_serves_the_app_shell_uncached(self, static_dir):
        async with TestClient(TestServer(create_app(static_dir=static_dir))) as client:
            response = await client.get("/")
            assert response.status == 200
            assert "Muse" in await response.text()
            assert response.headers["Cache-Control"] == "no-cache"

    @async_test
    async def test_dashboard_route_serves_the_app_shell(self, static_dir):
        # A refresh on a client-side route must not 404.
        async with TestClient(TestServer(create_app(static_dir=static_dir))) as client:
            response = await client.get("/dashboard")
            assert response.status == 200
            assert "Muse" in await response.text()

    @async_test
    async def test_service_worker_is_served_uncached(self, static_dir):
        async with TestClient(TestServer(create_app(static_dir=static_dir))) as client:
            response = await client.get("/sw.js")
            assert response.headers["Cache-Control"] == "no-cache"

    @async_test
    async def test_hashed_assets_are_served(self, static_dir):
        async with TestClient(TestServer(create_app(static_dir=static_dir))) as client:
            response = await client.get("/assets/app.js")
            assert response.status == 200

    @async_test
    async def test_unknown_seat_is_rejected(self, static_dir):
        app = create_app(static_dir=static_dir, hub=PlayerHub(seat_ids=make_seat_ids(2)))
        async with TestClient(TestServer(app)) as client:
            assert (await client.get("/ws/p9")).status == 404

    @async_test
    async def test_telemetry_reaches_the_hub(self, static_dir):
        hub = PlayerHub()
        async with TestClient(TestServer(create_app(static_dir=static_dir, hub=hub))) as client:
            async with client.ws_connect("/ws/p1") as socket:
                await socket.send_json({"rawScore": 0.5, "normalizedScore": -0.25})
                await _settle()
                assert hub.state_of("p1")["normalized"] == -0.25
                assert hub.occupied_seats == ("p1",)

    @async_test
    async def test_three_seats_stream_independently(self, static_dir):
        hub = PlayerHub(seat_ids=make_seat_ids(3))
        async with TestClient(TestServer(create_app(static_dir=static_dir, hub=hub))) as client:
            sockets = [await client.ws_connect(f"/ws/p{i}") for i in (1, 2, 3)]
            for i, socket in enumerate(sockets, start=1):
                await socket.send_json({"normalizedScore": i / 10})
            await _settle()

            assert hub.occupied_seats == ("p1", "p2", "p3")
            assert [round(hub.state_of(f"p{i}")["normalized"], 2) for i in (1, 2, 3)] == [0.1, 0.2, 0.3]
            for socket in sockets:
                await socket.close()

    @async_test
    async def test_malformed_frame_does_not_drop_the_connection(self, static_dir):
        hub = PlayerHub()
        async with TestClient(TestServer(create_app(static_dir=static_dir, hub=hub))) as client:
            async with client.ws_connect("/ws/p1") as socket:
                await socket.send_str("{half a frame")
                await socket.send_json({"rawScore": 1.0, "normalizedScore": 1.0})
                await _settle()
                assert not socket.closed
                assert hub.state_of("p1")["normalized"] == 1.0

    @async_test
    async def test_seat_is_released_on_disconnect(self, static_dir):
        hub = PlayerHub()
        async with TestClient(TestServer(create_app(static_dir=static_dir, hub=hub))) as client:
            async with client.ws_connect("/ws/p1") as socket:
                await _settle()
                assert hub.occupied_seats == ("p1",)
                await socket.close()
            await _settle()
            assert hub.occupied_seats == ()

    @async_test
    async def test_reconnect_displaces_the_stale_socket(self, static_dir):
        hub = PlayerHub()
        async with TestClient(TestServer(create_app(static_dir=static_dir, hub=hub))) as client:
            first = await client.ws_connect("/ws/p1")
            await _settle()
            second = await client.ws_connect("/ws/p1")
            await _settle()

            # The server closes the older socket rather than refusing the new
            # one, so a phone waking from sleep can always get its seat back.
            # The server also pushes recording state on connect, so skip past
            # any data frames to find the close.
            for _ in range(5):
                msg = await first.receive()
                if msg.type.name in {"CLOSE", "CLOSED", "CLOSING"}:
                    break
            else:
                pytest.fail("displaced socket was never closed")
            assert hub.occupied_seats == ("p1",)

            await second.send_json({"rawScore": 3.0, "normalizedScore": 0.75})
            await _settle()
            assert hub.state_of("p1")["normalized"] == 0.75
            await second.close()


class TestHttpRedirect:
    # A tablet typing the address without a scheme got ERR_CONNECTION_REFUSED,
    # because browsers do not upgrade bare addresses and nothing served port 80.

    @async_test
    async def test_plain_http_is_bounced_to_https(self):
        async with TestClient(TestServer(make_redirect_app(443))) as client:
            response = await client.get("/dashboard", allow_redirects=False)
            assert response.status == 302
            assert response.headers["Location"] == "https://127.0.0.1/dashboard"

    @async_test
    async def test_the_query_string_survives(self):
        async with TestClient(TestServer(make_redirect_app(443))) as client:
            response = await client.get("/?seat=p2", allow_redirects=False)
            assert response.headers["Location"] == "https://127.0.0.1/?seat=p2"

    @async_test
    async def test_a_nonstandard_https_port_is_carried_over(self):
        async with TestClient(TestServer(make_redirect_app(8443))) as client:
            response = await client.get("/", allow_redirects=False)
            assert response.headers["Location"] == "https://127.0.0.1:8443/"

    @async_test
    async def test_every_path_and_method_redirects(self):
        # Whatever a device asks for, the answer is "same thing, over TLS".
        async with TestClient(TestServer(make_redirect_app(443))) as client:
            for path in ("/", "/dashboard", "/healthz", "/assets/app.js"):
                response = await client.get(path, allow_redirects=False)
                assert response.status == 302, path
                assert response.headers["Location"].startswith("https://"), path
            posted = await client.post("/api/session/start", allow_redirects=False)
            assert posted.status == 302

    @async_test
    async def test_redirect_is_temporary_not_permanent(self):
        # A permanent redirect gets cached hard by every phone that hits it,
        # which is not something you want to undo on someone else's device.
        async with TestClient(TestServer(make_redirect_app(443))) as client:
            response = await client.get("/", allow_redirects=False)
            assert response.status == 302


class TestObserver:
    @async_test
    async def test_observer_gets_a_snapshot_immediately(self, static_dir):
        # Without this the dashboard would stay blank until the first tick.
        hub = PlayerHub(seat_ids=make_seat_ids(3))
        async with TestClient(TestServer(create_app(static_dir=static_dir, hub=hub))) as client:
            async with client.ws_connect("/ws/observe") as observer:
                payload = json.loads((await observer.receive()).data)
                assert payload["type"] == "state"
                assert [s["id"] for s in payload["seats"]] == ["p1", "p2", "p3"]

    @async_test
    async def test_observer_sees_player_telemetry(self, static_dir):
        hub = PlayerHub(seat_ids=make_seat_ids(2))
        async with TestClient(TestServer(create_app(static_dir=static_dir, hub=hub))) as client:
            async with client.ws_connect("/ws/observe") as observer:
                await observer.receive()  # the immediate snapshot

                async with client.ws_connect("/ws/p2") as player:
                    await player.send_json({"normalizedScore": 0.8})

                    # Wait for a broadcast tick rather than the immediate send.
                    for _ in range(20):
                        payload = json.loads((await observer.receive()).data)
                        seat = next(s for s in payload["seats"] if s["id"] == "p2")
                        if seat["connected"] and seat["normalized"] == 0.8:
                            break
                    else:
                        pytest.fail("observer never saw p2 telemetry")

    @async_test
    async def test_observer_is_not_a_seat(self, static_dir):
        # /ws/observe must not be routed as a seat named "observe".
        hub = PlayerHub()
        async with TestClient(TestServer(create_app(static_dir=static_dir, hub=hub))) as client:
            async with client.ws_connect("/ws/observe") as observer:
                await observer.receive()
                assert hub.occupied_seats == ()

    @async_test
    async def test_observer_frames_are_ignored(self, static_dir):
        hub = PlayerHub()
        async with TestClient(TestServer(create_app(static_dir=static_dir, hub=hub))) as client:
            async with client.ws_connect("/ws/observe") as observer:
                await observer.receive()
                await observer.send_json({"rawScore": 99.0, "normalizedScore": 99.0})
                await _settle()
                assert hub.state_of("p1")["normalized"] == 0.0
                assert not observer.closed

    @async_test
    async def test_observer_is_forgotten_on_disconnect(self, static_dir):
        hub = PlayerHub()
        async with TestClient(TestServer(create_app(static_dir=static_dir, hub=hub))) as client:
            async with client.ws_connect("/ws/observe") as observer:
                await observer.receive()
                assert len(hub.observers) == 1
                await observer.close()
            await _settle()
            assert len(hub.observers) == 0


class TestRawOverTheWire:
    @async_test
    async def test_a_player_is_told_the_session_state_on_connect(self, static_dir, recorder):
        # A phone joining mid-session must start streaming immediately, or its
        # data is missing from a recording that looks complete.
        hub = PlayerHub(seat_ids=make_seat_ids(2), recorder=recorder)
        app = create_app(static_dir=static_dir, hub=hub)
        async with TestClient(TestServer(app)) as client:
            await client.post("/api/session/start", json={"label": "already-going"})
            async with client.ws_connect("/ws/p1") as socket:
                msg = json.loads((await socket.receive()).data)
                assert msg == {
                    "type": "recording", "active": True, "label": "already-going",
                }
            await client.post("/api/session/stop")

    @async_test
    async def test_players_are_told_when_a_session_starts_and_stops(self, static_dir, recorder):
        hub = PlayerHub(seat_ids=make_seat_ids(2), recorder=recorder)
        app = create_app(static_dir=static_dir, hub=hub)
        async with TestClient(TestServer(app)) as client:
            async with client.ws_connect("/ws/p1") as socket:
                first = json.loads((await socket.receive()).data)
                assert first["active"] is False

                await client.post("/api/session/start", json={"label": "go"})
                started = json.loads((await socket.receive()).data)
                assert started == {"type": "recording", "active": True, "label": "go"}

                await client.post("/api/session/stop")
                stopped = json.loads((await socket.receive()).data)
                assert stopped["active"] is False

    @async_test
    async def test_a_raw_batch_reaches_disk_over_a_websocket(self, static_dir, recorder):
        hub = PlayerHub(seat_ids=make_seat_ids(2), recorder=recorder)
        app = create_app(static_dir=static_dir, hub=hub)
        async with TestClient(TestServer(app)) as client:
            started = await (
                await client.post("/api/session/start", json={"label": "wire"})
            ).json()

            async with client.ws_connect("/ws/p2") as socket:
                await socket.receive()  # recording announcement
                await socket.send_str(
                    _raw_batch(
                        eeg=[[1.0, 2.0, 3.0, 4.0], [5.0, 6.0, 7.0, 8.0]],
                        acc=[[0.0, 0.0, 1.0]],
                        overflows={"eeg": 3},
                    )
                )
                await _settle(0.2)

            await client.post("/api/session/stop")

        session = pathlib.Path(started["dir"])
        eeg = _read_csv(session / "eeg.csv")
        assert [r[1] for r in eeg[1:]] == ["p2", "p2"]
        assert _read_csv(session / "acc.csv")[1][1:] == ["p2", "0.0", "0.0", "1.0"]
        assert hub.overflows_for("p2") == 3
        meta = json.loads((session / "meta.json").read_text())
        assert meta["errors"] == 0


class TestSessionControl:
    @async_test
    async def test_start_and_stop_a_session(self, static_dir, recorder):
        hub = PlayerHub(seat_ids=make_seat_ids(2), recorder=recorder)
        async with TestClient(TestServer(create_app(static_dir=static_dir, hub=hub))) as client:
            started = await (await client.post("/api/session/start", json={"label": "trial"})).json()
            assert started["active"] is True
            assert started["label"] == "trial"
            assert hub.recording is True

            stopped = await (await client.post("/api/session/stop")).json()
            assert stopped["active"] is False
            assert stopped["dir"] == started["dir"]
            assert hub.recording is False

    @async_test
    async def test_session_start_without_a_label_still_works(self, static_dir, recorder):
        hub = PlayerHub(recorder=recorder)
        async with TestClient(TestServer(create_app(static_dir=static_dir, hub=hub))) as client:
            response = await client.post("/api/session/start")
            assert response.status == 200
            assert hub.recording is True

    @async_test
    async def test_recording_state_appears_in_the_snapshot(self, static_dir, recorder):
        # Every dashboard should reflect the session, not just the one that
        # pressed the button.
        hub = PlayerHub(recorder=recorder)
        async with TestClient(TestServer(create_app(static_dir=static_dir, hub=hub))) as client:
            await client.post("/api/session/start", json={"label": "shared"})
            body = await (await client.get("/healthz")).json()
            assert body["recording"] == {"active": True, "label": "shared"}

    @async_test
    async def test_session_start_without_a_recorder_reports_unavailable(self, static_dir):
        hub = PlayerHub()  # no recorder
        async with TestClient(TestServer(create_app(static_dir=static_dir, hub=hub))) as client:
            assert (await client.post("/api/session/start")).status == 503

    @async_test
    async def test_a_live_session_captures_socket_telemetry(self, static_dir, recorder):
        hub = PlayerHub(seat_ids=make_seat_ids(2), recorder=recorder)
        async with TestClient(TestServer(create_app(static_dir=static_dir, hub=hub))) as client:
            started = await (await client.post("/api/session/start", json={"label": "live"})).json()

            async with client.ws_connect("/ws/p1") as player:
                await player.send_json({"rawScore": 2.0, "normalizedScore": 0.9})
                await _settle()

            await client.post("/api/session/stop")

            rows = _read_csv(recorder.base_dir / started["dir"].split("/")[-1] / "telemetry.csv")
            assert [r[1] for r in rows[1:]] == ["p1"]
            assert rows[1][2:] == ["2.0", "0.9", "0"]


class FakeLight:
    """Stands in for a FocusLight so the broadcast loop can be tested off-wire."""

    def __init__(self):
        self.snapshots = []

    def update(self, snapshot):
        self.snapshots.append(snapshot)


class _NullSocket:
    """Swallows datagrams so a test never touches the network."""

    def sendto(self, payload, addr):
        pass

    def close(self):
        pass


class TestLightOutput:
    @async_test
    async def test_light_is_driven_by_the_broadcast_loop(self, static_dir):
        hub = PlayerHub(seat_ids=make_seat_ids(2))
        light = FakeLight()
        app = create_app(static_dir=static_dir, hub=hub, light=light)
        async with TestClient(TestServer(app)) as client:
            async with client.ws_connect("/ws/p1") as player:
                await player.send_json({"normalizedScore": 0.9})
                await _settle(0.3)

        seen = [
            seat for snap in light.snapshots
            for seat in snap["seats"]
            if seat["id"] == "p1" and seat["normalized"] == 0.9
        ]
        assert seen, "light never saw p1 telemetry"

    @async_test
    async def test_light_runs_with_no_dashboard_connected(self, static_dir):
        # The strip has to keep working when nobody has the dashboard open, so
        # driving it must not be gated on there being an observer attached.
        hub = PlayerHub(seat_ids=make_seat_ids(2))
        light = FakeLight()
        app = create_app(static_dir=static_dir, hub=hub, light=light)
        async with TestClient(TestServer(app)) as client:
            assert (await client.get("/healthz")).status == 200
            await _settle(0.3)

        assert light.snapshots, "light was never driven without an observer"

    @async_test
    async def test_server_runs_without_a_light(self, static_dir):
        hub = PlayerHub(seat_ids=make_seat_ids(2))
        async with TestClient(TestServer(create_app(static_dir=static_dir, hub=hub))) as client:
            await _settle(0.15)
            assert (await client.get("/healthz")).status == 200


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


class TestLightArgs:
    def test_lights_are_off_by_default(self):
        assert parse_args([]).led_host is None

    def test_led_host_enables_the_strip(self):
        args = parse_args(["--led-host", "10.0.0.5"])
        assert args.led_host == "10.0.0.5"

    def test_led_count_and_seat_are_configurable(self):
        args = parse_args(["--led-host", "10.0.0.5", "--led-count", "144", "--led-seat", "p2"])
        assert args.led_count == 144
        assert args.led_seat == "p2"

    def test_led_seat_defaults_to_the_first_seat(self):
        assert parse_args([]).led_seat == "p1"


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
