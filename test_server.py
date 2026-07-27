import asyncio
import functools
import json
import ssl
import subprocess

import pytest
from aiohttp.test_utils import TestClient, TestServer

from server import (
    CALIBRATED,
    IGNORED,
    TELEMETRY,
    PlayerHub,
    build_ssl_context,
    create_app,
)


def async_test(fn):
    """Run an async test body without pulling in an asyncio pytest plugin."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))

    return wrapper


@pytest.fixture
def static_dir(tmp_path):
    """A stand-in for muse-pwa/dist."""
    (tmp_path / "index.html").write_text("<!doctype html><title>Muse</title>")
    (tmp_path / "sw.js").write_text("// service worker")
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "app.js").write_text("export const x = 1;")
    return tmp_path


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


class TestPlayerHub:
    def test_only_known_seats_are_accepted(self):
        hub = PlayerHub()
        assert hub.is_known("p1")
        assert hub.is_known("p2")
        assert not hub.is_known("p3")
        assert not hub.is_known("../etc/passwd")

    def test_telemetry_updates_state(self):
        hub = PlayerHub()
        outcome = hub.handle_message(
            "p1",
            json.dumps({"rawScore": 1.5, "normalizedScore": 0.25, "isCalibrating": True}),
        )
        assert outcome == TELEMETRY
        assert hub.state_of("p1") == {"raw": 1.5, "normalized": 0.25, "calibrating": True}

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
        # The previous server formatted baseline with :.4f and blew up when the
        # field was absent.
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
        assert hub.state_of("p1") == {"raw": 0.0, "normalized": 0.0, "calibrating": False}

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

    def test_snapshot_does_not_alias_internal_state(self):
        hub = PlayerHub()
        snapshot = hub.snapshot()
        snapshot["p1"]["raw"] = 99.0
        assert hub.state_of("p1")["raw"] == 0.0


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
    async def test_health_reports_seats(self, static_dir):
        async with TestClient(TestServer(create_app(static_dir=static_dir))) as client:
            response = await client.get("/healthz")
            assert response.status == 200
            body = await response.json()
            assert body["status"] == "ok"
            assert body["seats"] == []

    @async_test
    async def test_root_serves_the_app_shell_uncached(self, static_dir):
        async with TestClient(TestServer(create_app(static_dir=static_dir))) as client:
            response = await client.get("/")
            assert response.status == 200
            assert "Muse" in await response.text()
            assert response.headers["Cache-Control"] == "no-cache"

    @async_test
    async def test_service_worker_is_served_uncached(self, static_dir):
        async with TestClient(TestServer(create_app(static_dir=static_dir))) as client:
            response = await client.get("/sw.js")
            assert response.status == 200
            assert response.headers["Cache-Control"] == "no-cache"

    @async_test
    async def test_hashed_assets_are_served(self, static_dir):
        async with TestClient(TestServer(create_app(static_dir=static_dir))) as client:
            response = await client.get("/assets/app.js")
            assert response.status == 200
            assert "export const x" in await response.text()

    @async_test
    async def test_unknown_seat_is_rejected(self, static_dir):
        async with TestClient(TestServer(create_app(static_dir=static_dir))) as client:
            response = await client.get("/ws/p9")
            assert response.status == 404

    @async_test
    async def test_telemetry_reaches_the_hub(self, static_dir):
        hub = PlayerHub()
        app = create_app(static_dir=static_dir, hub=hub)
        async with TestClient(TestServer(app)) as client:
            async with client.ws_connect("/ws/p1") as socket:
                await socket.send_json({"rawScore": 0.5, "normalizedScore": -0.25})
                await _settle()
                assert hub.state_of("p1")["normalized"] == -0.25
                assert hub.occupied_seats == ("p1",)

    @async_test
    async def test_malformed_frame_does_not_drop_the_connection(self, static_dir):
        hub = PlayerHub()
        app = create_app(static_dir=static_dir, hub=hub)
        async with TestClient(TestServer(app)) as client:
            async with client.ws_connect("/ws/p1") as socket:
                await socket.send_str("{half a frame")
                await socket.send_json({"rawScore": 1.0, "normalizedScore": 1.0})
                await _settle()
                assert not socket.closed
                assert hub.state_of("p1")["normalized"] == 1.0

    @async_test
    async def test_seat_is_released_on_disconnect(self, static_dir):
        hub = PlayerHub()
        app = create_app(static_dir=static_dir, hub=hub)
        async with TestClient(TestServer(app)) as client:
            async with client.ws_connect("/ws/p1") as socket:
                await _settle()
                assert hub.occupied_seats == ("p1",)
                await socket.close()
            await _settle()
            assert hub.occupied_seats == ()

    @async_test
    async def test_reconnect_displaces_the_stale_socket(self, static_dir):
        hub = PlayerHub()
        app = create_app(static_dir=static_dir, hub=hub)
        async with TestClient(TestServer(app)) as client:
            first = await client.ws_connect("/ws/p1")
            await _settle()
            second = await client.ws_connect("/ws/p1")
            await _settle()

            # The server closes the older socket rather than refusing the new
            # one, so a phone waking from sleep can always get its seat back.
            assert (await first.receive()).type.name in {"CLOSE", "CLOSED", "CLOSING"}
            assert hub.occupied_seats == ("p1",)

            await second.send_json({"rawScore": 3.0, "normalizedScore": 0.75})
            await _settle()
            assert hub.state_of("p1")["normalized"] == 0.75
            await second.close()


async def _settle():
    """Yield long enough for the server side of a socket to process a frame."""
    await asyncio.sleep(0.05)
