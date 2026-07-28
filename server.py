"""Single-origin host for the Muse PWA, its player sockets, and the operator view.

The PWA needs a secure context: Web Bluetooth and service worker registration
are both unavailable over plain HTTP on a LAN address. Serving the app and the
websockets from one TLS origin gets both under a single certificate and lets the
page derive its socket URL from window.location.

Players connect to /ws/{seat} and stream focus scores. The dashboard connects to
/ws/observe and receives whole-state snapshots. Recording is driven over
/api/session/* and written through SessionRecorder.

Run with --cert/--key (or MUSE_TLS_CERT/MUSE_TLS_KEY) to serve HTTPS. With
neither, it serves plain HTTP, which is only useful via localhost.
"""

import argparse
import asyncio
import contextlib
import json
import logging
import os
import ssl
import time
from pathlib import Path

from aiohttp import WSCloseCode, WSMsgType, web

from session_recorder import SessionRecorder

logger = logging.getLogger("muse.server")

DEFAULT_SEAT_COUNT = 4
DEFAULT_STATIC_DIR = Path(__file__).resolve().parent / "muse-pwa" / "dist"
DEFAULT_RECORDINGS_DIR = Path(__file__).resolve().parent / "recordings"
DEFAULT_PORT = 8443

# The dashboard is for human eyes; it does not need the 30 Hz players stream at.
OBSERVER_HZ = 10

# How long a seat may hold its socket open without sending anything before the
# dashboard stops calling it live. Clients stream at 30 Hz, so this is a very
# wide margin — it only trips when data has genuinely stopped.
STALE_AFTER_S = 3.0

HUB = web.AppKey("hub")
STATIC_DIR = web.AppKey("static_dir", Path)
BROADCAST_TASK = web.AppKey("broadcast_task")

# Outcomes of handling one websocket frame.
TELEMETRY = "telemetry"
CALIBRATED = "calibrated"
IGNORED = "ignored"

# Synthetic addresses for SessionRecorder. These are not OSC addresses off a
# headset; see the FIXED table in session_recorder.py.
ADDR_TELEMETRY = "/pwa/telemetry"
ADDR_CALIBRATION = "/pwa/calibration"
ADDR_SEAT = "/pwa/seat"


def make_seat_ids(count):
    if count < 1:
        raise ValueError("need at least one seat")
    return tuple(f"p{i}" for i in range(1, count + 1))


def _blank_state():
    return {"raw": 0.0, "normalized": 0.0, "calibrating": False}


class PlayerHub:
    """Tracks seat occupancy, the latest scores, observers, and the recorder.

    Holds no aiohttp imports beyond the opaque socket objects it is handed, so
    its state transitions are testable without standing up a server.
    """

    def __init__(self, seat_ids=None, recorder=None, clock=None):
        self.seat_ids = tuple(seat_ids) if seat_ids else make_seat_ids(DEFAULT_SEAT_COUNT)
        self._sockets = {}
        self._states = {sid: _blank_state() for sid in self.seat_ids}
        self._observers = set()
        self._recorder = recorder
        self._label = ""
        # Injectable so staleness is testable without waiting in real time.
        self._clock = clock or time.monotonic
        self._last_seen = {sid: None for sid in self.seat_ids}

    # -- seats ---------------------------------------------------------------

    def is_known(self, seat_id):
        return seat_id in self.seat_ids

    def attach(self, seat_id, socket):
        """Claim a seat. Returns the socket this one displaced, if any."""
        displaced = self._sockets.get(seat_id)
        self._sockets[seat_id] = socket
        # Start the staleness clock at connect, so a seat that never sends
        # anything goes stale rather than sitting at a default forever.
        self._last_seen[seat_id] = self._clock()
        self._record(ADDR_SEAT, [seat_id, "connect"])
        return displaced

    def detach(self, seat_id, socket):
        """Release a seat, but only if this socket still holds it.

        A phone that reconnects after its screen slept leaves a dead socket
        behind whose handler unwinds *after* the replacement has registered.
        Without the identity check that late unwind would evict the live one.
        """
        if self._sockets.get(seat_id) is socket:
            del self._sockets[seat_id]
            self._last_seen[seat_id] = None
            self._record(ADDR_SEAT, [seat_id, "disconnect"])
            return True
        return False

    def handle_message(self, seat_id, raw_message):
        """Apply one raw frame, returning which kind of frame it was.

        Anything unparseable is reported as IGNORED rather than raising: a
        half-written frame from a phone that lost signal should not tear down
        the connection.
        """
        try:
            data = json.loads(raw_message)
        except (json.JSONDecodeError, TypeError, ValueError):
            return IGNORED

        if not isinstance(data, dict):
            return IGNORED

        if data.get("event") == "calibration_complete":
            # Worth recording: a normalized score cannot be interpreted later
            # without the baseline and half-range it was derived from.
            try:
                baseline = float(data.get("baseline", 0.0))
                half_range = float(data.get("halfRange", 0.0))
            except (TypeError, ValueError):
                return IGNORED
            self._record(ADDR_CALIBRATION, [seat_id, baseline, half_range])
            return CALIBRATED

        try:
            state = {
                "raw": float(data.get("rawScore", 0.0)),
                "normalized": float(data.get("normalizedScore", 0.0)),
                "calibrating": bool(data.get("isCalibrating", False)),
            }
        except (TypeError, ValueError):
            return IGNORED

        # Only a *changed* reading counts as liveness. A client whose headset
        # dropped keeps streaming its last computed score at 30 Hz, so frames
        # arriving is not evidence of a live headset — the value moving is.
        # Real EEG-derived scores never repeat bit-for-bit, so an identical
        # reading means the source is frozen.
        if state != self._states[seat_id]:
            self._last_seen[seat_id] = self._clock()
        self._states[seat_id] = state
        self._record(
            ADDR_TELEMETRY,
            [seat_id, state["raw"], state["normalized"], int(state["calibrating"])],
        )
        return TELEMETRY

    def state_of(self, seat_id):
        return dict(self._states[seat_id])

    @property
    def occupied_seats(self):
        return tuple(sorted(self._sockets))

    # -- observers -----------------------------------------------------------

    def add_observer(self, socket):
        self._observers.add(socket)

    def remove_observer(self, socket):
        self._observers.discard(socket)

    @property
    def observers(self):
        return frozenset(self._observers)

    # -- recording -----------------------------------------------------------

    @property
    def recording(self):
        return bool(self._recorder is not None and self._recorder.active)

    def start_recording(self, label):
        if self._recorder is None:
            return None
        self._label = label
        directory = self._recorder.start(label, extra_meta={"source": "pwa-websocket"})
        # Seats already occupied would otherwise have no connect event in the
        # recording, leaving their telemetry apparently unexplained.
        for seat_id in self.occupied_seats:
            self._record(ADDR_SEAT, [seat_id, "connect"])
        return directory

    def stop_recording(self):
        if self._recorder is None:
            return None
        return self._recorder.stop()

    def _record(self, address, values):
        """Best-effort recording. Never let it break a player connection."""
        if self._recorder is None or not self._recorder.active:
            return
        try:
            self._recorder.record(address, values)
        except Exception:
            logger.exception("recording %s failed", address)

    # -- snapshot ------------------------------------------------------------

    def is_stale(self, seat_id):
        """True when a seat holds its socket open but has stopped sending.

        An open socket is not evidence of a live headset. A phone whose screen
        slept, a backgrounded tab, or a Muse that fell off all leave the
        websocket up while telemetry stops, and the dashboard would otherwise
        keep showing the last value it ever saw as though it were current.
        """
        if seat_id not in self._sockets:
            return False
        last = self._last_seen.get(seat_id)
        if last is None:
            return True
        return (self._clock() - last) > STALE_AFTER_S

    def snapshot(self):
        """Whole state, as broadcast to observers and returned by /healthz.

        Deliberately not a delta. The payload is small, so a dashboard that
        connects late or reconnects after a laptop sleeps is immediately
        correct with no replay logic.
        """
        return {
            "type": "state",
            "seats": [
                dict(
                    self._states[sid],
                    id=sid,
                    connected=sid in self._sockets,
                    stale=self.is_stale(sid),
                )
                for sid in self.seat_ids
            ],
            "recording": {"active": self.recording, "label": self._label},
        }


def build_ssl_context(cert_path, key_path):
    """Build a TLS context, or None when no certificate is configured.

    Half a configuration is an error rather than a fallback. Quietly serving
    HTTP because the key path had a typo would surface as Web Bluetooth simply
    not existing on every phone, which is the exact failure this server exists
    to prevent.
    """
    if not cert_path and not key_path:
        return None

    if not (cert_path and key_path):
        missing = "key" if cert_path else "certificate"
        raise ValueError(
            f"TLS is half configured: the {missing} is missing. "
            "Set both MUSE_TLS_CERT and MUSE_TLS_KEY, or neither."
        )

    for label, path in (("certificate", cert_path), ("key", key_path)):
        if not Path(path).is_file():
            raise FileNotFoundError(f"TLS {label} not found: {path}")

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_path, key_path)
    return context


# -- handlers ----------------------------------------------------------------


async def handle_health(request):
    return web.json_response(request.app[HUB].snapshot())


async def handle_seats(request):
    return web.json_response({"seats": list(request.app[HUB].seat_ids)})


async def handle_session_start(request):
    hub = request.app[HUB]
    try:
        body = await request.json()
    except Exception:
        body = {}

    label = (body or {}).get("label") or "session"
    directory = hub.start_recording(label)
    if directory is None:
        raise web.HTTPServiceUnavailable(text="No recorder is configured")

    logger.info("Recording started: %s", directory)
    return web.json_response({"active": True, "label": label, "dir": str(directory)})


async def handle_session_stop(request):
    hub = request.app[HUB]
    directory = hub.stop_recording()
    logger.info("Recording stopped: %s", directory)
    return web.json_response({"active": False, "dir": str(directory) if directory else None})


async def handle_observer(request):
    """A read-only view of the whole hub.

    Frames sent by an observer are ignored: the dashboard drives sessions over
    HTTP so there is one command path rather than two.
    """
    hub = request.app[HUB]
    socket = web.WebSocketResponse(heartbeat=30)
    await socket.prepare(request)

    hub.add_observer(socket)
    logger.info("Observer connected from %s", request.remote)

    # Send one immediately so the dashboard paints without waiting for a tick.
    with contextlib.suppress(Exception):
        await socket.send_str(json.dumps(hub.snapshot()))

    try:
        async for message in socket:
            if message.type is WSMsgType.ERROR:
                break
    finally:
        hub.remove_observer(socket)
        logger.info("Observer disconnected")

    return socket


async def handle_player(request):
    seat_id = request.match_info["seat_id"]
    hub = request.app[HUB]

    if not hub.is_known(seat_id):
        raise web.HTTPNotFound(text=f"Unknown seat: {seat_id}")

    socket = web.WebSocketResponse(heartbeat=30)
    await socket.prepare(request)

    displaced = hub.attach(seat_id, socket)
    if displaced is not None and not displaced.closed:
        await displaced.close(
            code=WSCloseCode.GOING_AWAY, message=b"replaced by a newer connection"
        )
        logger.info("Seat %s reconnected, dropping the stale socket", seat_id)

    logger.info("Seat %s connected from %s", seat_id, request.remote)

    try:
        async for message in socket:
            if message.type is WSMsgType.ERROR:
                break
            if message.type is not WSMsgType.TEXT:
                continue

            outcome = hub.handle_message(seat_id, message.data)
            if outcome == CALIBRATED:
                logger.info("Seat %s calibrated", seat_id)
            elif outcome == TELEMETRY:
                # Per-frame logging at 30 Hz would flood the journal.
                logger.debug("%s %s", seat_id, hub.state_of(seat_id))
    finally:
        if hub.detach(seat_id, socket):
            logger.info("Seat %s disconnected", seat_id)

    return socket


def _no_cache_file(filename):
    """Serve one file from the static dir without letting it be cached.

    index.html and sw.js decide which build every phone runs. If a phone caches
    them it can pin itself to an old bundle with no way to bust it short of
    clearing site data.
    """

    async def handler(request):
        path = request.app[STATIC_DIR] / filename
        if not path.is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(path, headers={"Cache-Control": "no-cache"})

    return handler


# -- broadcast ---------------------------------------------------------------


async def _broadcast_loop(app):
    hub = app[HUB]
    interval = 1.0 / OBSERVER_HZ

    while True:
        await asyncio.sleep(interval)

        observers = tuple(hub.observers)
        if not observers:
            continue

        payload = json.dumps(hub.snapshot())
        # Send concurrently so one observer on a bad link cannot pace the loop
        # for everyone else.
        results = await asyncio.gather(
            *(observer.send_str(payload) for observer in observers),
            return_exceptions=True,
        )
        for observer, result in zip(observers, results):
            if isinstance(result, BaseException):
                hub.remove_observer(observer)


async def _start_broadcast(app):
    app[BROADCAST_TASK] = asyncio.create_task(_broadcast_loop(app))


async def _stop_broadcast(app):
    task = app.get(BROADCAST_TASK)
    if task is None:
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


def create_app(static_dir=DEFAULT_STATIC_DIR, hub=None, recorder=None):
    app = web.Application()
    app[HUB] = hub if hub is not None else PlayerHub(recorder=recorder)
    app[STATIC_DIR] = Path(static_dir)

    # Registration order is resolution order. /ws/observe has to precede the
    # seat pattern or it would be matched as a seat named "observe", and the
    # static catch-all has to come last or it swallows everything.
    app.router.add_get("/healthz", handle_health)
    app.router.add_get("/api/seats", handle_seats)
    app.router.add_post("/api/session/start", handle_session_start)
    app.router.add_post("/api/session/stop", handle_session_stop)
    app.router.add_get("/ws/observe", handle_observer)
    app.router.add_get("/ws/{seat_id}", handle_player)

    # The dashboard is a client-side route, so a refresh on it must still get
    # the app shell rather than a 404.
    app.router.add_get("/", _no_cache_file("index.html"))
    app.router.add_get("/index.html", _no_cache_file("index.html"))
    app.router.add_get("/dashboard", _no_cache_file("index.html"))
    app.router.add_get("/sw.js", _no_cache_file("sw.js"))

    if app[STATIC_DIR].is_dir():
        app.router.add_static("/", app[STATIC_DIR])

    app.on_startup.append(_start_broadcast)
    app.on_cleanup.append(_stop_broadcast)
    return app


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Serve the Muse PWA over HTTPS.")
    parser.add_argument("--host", default=os.environ.get("MUSE_HOST", "0.0.0.0"))
    parser.add_argument(
        "--port", type=int, default=int(os.environ.get("MUSE_PORT", DEFAULT_PORT))
    )
    parser.add_argument(
        "--seats",
        type=int,
        default=int(os.environ.get("MUSE_SEATS", DEFAULT_SEAT_COUNT)),
        help="number of player seats, named p1..pN",
    )
    parser.add_argument("--cert", default=os.environ.get("MUSE_TLS_CERT"))
    parser.add_argument("--key", default=os.environ.get("MUSE_TLS_KEY"))
    parser.add_argument(
        "--static-dir",
        type=Path,
        default=Path(os.environ.get("MUSE_STATIC_DIR", DEFAULT_STATIC_DIR)),
    )
    parser.add_argument(
        "--recordings-dir",
        type=Path,
        default=Path(os.environ.get("MUSE_RECORDINGS_DIR", DEFAULT_RECORDINGS_DIR)),
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    ssl_context = build_ssl_context(args.cert, args.key)

    if not args.static_dir.is_dir():
        raise SystemExit(
            f"No PWA build at {args.static_dir}. Run `npm run build` in muse-pwa/ first."
        )

    if ssl_context is None:
        logger.warning(
            "No certificate configured, serving plain HTTP. Web Bluetooth will "
            "only work through localhost, not from another device."
        )

    hub = PlayerHub(
        seat_ids=make_seat_ids(args.seats),
        recorder=SessionRecorder(base_dir=args.recordings_dir),
    )

    scheme = "https" if ssl_context else "http"
    logger.info(
        "Serving %s on %s://%s:%s with seats %s",
        args.static_dir,
        scheme,
        args.host,
        args.port,
        ", ".join(hub.seat_ids),
    )

    web.run_app(
        create_app(static_dir=args.static_dir, hub=hub),
        host=args.host,
        port=args.port,
        ssl_context=ssl_context,
        print=None,
    )


if __name__ == "__main__":
    main()
