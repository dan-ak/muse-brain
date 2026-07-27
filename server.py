"""Single-origin host for the Muse PWA and its player telemetry websockets.

The PWA needs a secure context: Web Bluetooth and service worker registration
are both unavailable over plain HTTP on a LAN address. Serving the app and the
websocket from one TLS origin gets both under a single certificate and lets the
page derive its socket URL from window.location.

Run with --cert/--key (or MUSE_TLS_CERT/MUSE_TLS_KEY) to serve HTTPS. With
neither, it serves plain HTTP, which is only useful via localhost.
"""

import argparse
import json
import logging
import os
import ssl
from pathlib import Path

from aiohttp import WSCloseCode, WSMsgType, web

logger = logging.getLogger("muse.server")

DEFAULT_PLAYER_IDS = ("p1", "p2")
DEFAULT_STATIC_DIR = Path(__file__).resolve().parent / "muse-pwa" / "dist"
DEFAULT_PORT = 8443

HUB = web.AppKey("hub")
STATIC_DIR = web.AppKey("static_dir", Path)

# Outcomes of handling one websocket frame.
TELEMETRY = "telemetry"
CALIBRATED = "calibrated"
IGNORED = "ignored"


def _blank_state():
    return {"raw": 0.0, "normalized": 0.0, "calibrating": False}


class PlayerHub:
    """Tracks which player seats are occupied and their latest scores.

    Deliberately free of aiohttp imports so the state transitions can be tested
    without standing up a server.
    """

    def __init__(self, player_ids=DEFAULT_PLAYER_IDS):
        self.player_ids = tuple(player_ids)
        self._sockets = {}
        self._states = {pid: _blank_state() for pid in self.player_ids}

    def is_known(self, player_id):
        return player_id in self.player_ids

    def attach(self, player_id, socket):
        """Claim a seat. Returns the socket this one displaced, if any."""
        displaced = self._sockets.get(player_id)
        self._sockets[player_id] = socket
        return displaced

    def detach(self, player_id, socket):
        """Release a seat, but only if this socket still holds it.

        A phone that reconnects after its screen slept leaves a dead socket
        behind whose handler unwinds *after* the replacement has registered.
        Without the identity check that late unwind would evict the live one.
        """
        if self._sockets.get(player_id) is socket:
            del self._sockets[player_id]
            return True
        return False

    def handle_message(self, player_id, raw_message):
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
            return CALIBRATED

        try:
            self._states[player_id] = {
                "raw": float(data.get("rawScore", 0.0)),
                "normalized": float(data.get("normalizedScore", 0.0)),
                "calibrating": bool(data.get("isCalibrating", False)),
            }
        except (TypeError, ValueError):
            return IGNORED

        return TELEMETRY

    def state_of(self, player_id):
        return dict(self._states[player_id])

    def snapshot(self):
        return {pid: dict(state) for pid, state in self._states.items()}

    @property
    def occupied_seats(self):
        return tuple(sorted(self._sockets))


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


async def handle_health(request):
    hub = request.app[HUB]
    return web.json_response(
        {"status": "ok", "seats": list(hub.occupied_seats), "players": hub.snapshot()}
    )


async def handle_websocket(request):
    player_id = request.match_info["player_id"]
    hub = request.app[HUB]

    if not hub.is_known(player_id):
        raise web.HTTPNotFound(text=f"Unknown player seat: {player_id}")

    socket = web.WebSocketResponse(heartbeat=30)
    await socket.prepare(request)

    displaced = hub.attach(player_id, socket)
    if displaced is not None and not displaced.closed:
        await displaced.close(
            code=WSCloseCode.GOING_AWAY, message=b"replaced by a newer connection"
        )
        logger.info("Player %s reconnected, dropping the stale socket", player_id.upper())

    logger.info("Player %s connected from %s", player_id.upper(), request.remote)

    try:
        async for message in socket:
            if message.type is WSMsgType.ERROR:
                break
            if message.type is not WSMsgType.TEXT:
                continue

            outcome = hub.handle_message(player_id, message.data)
            if outcome == CALIBRATED:
                logger.info("Player %s calibrated", player_id.upper())
            elif outcome == TELEMETRY:
                # Per-frame logging at 30 Hz would flood the journal.
                logger.debug("%s %s", player_id, hub.state_of(player_id))
    finally:
        if hub.detach(player_id, socket):
            logger.info("Player %s disconnected", player_id.upper())

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


def create_app(static_dir=DEFAULT_STATIC_DIR, hub=None):
    app = web.Application()
    app[HUB] = hub if hub is not None else PlayerHub()
    app[STATIC_DIR] = Path(static_dir)

    # Registration order is resolution order: the specific routes have to be
    # added before the catch-all static handler or it swallows them.
    app.router.add_get("/healthz", handle_health)
    app.router.add_get("/ws/{player_id}", handle_websocket)
    app.router.add_get("/", _no_cache_file("index.html"))
    app.router.add_get("/index.html", _no_cache_file("index.html"))
    app.router.add_get("/sw.js", _no_cache_file("sw.js"))

    if app[STATIC_DIR].is_dir():
        app.router.add_static("/", app[STATIC_DIR])

    return app


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Serve the Muse PWA over HTTPS.")
    parser.add_argument("--host", default=os.environ.get("MUSE_HOST", "0.0.0.0"))
    parser.add_argument(
        "--port", type=int, default=int(os.environ.get("MUSE_PORT", DEFAULT_PORT))
    )
    parser.add_argument("--cert", default=os.environ.get("MUSE_TLS_CERT"))
    parser.add_argument("--key", default=os.environ.get("MUSE_TLS_KEY"))
    parser.add_argument(
        "--static-dir",
        type=Path,
        default=Path(os.environ.get("MUSE_STATIC_DIR", DEFAULT_STATIC_DIR)),
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

    scheme = "https" if ssl_context else "http"
    logger.info("Serving %s on %s://%s:%s", args.static_dir, scheme, args.host, args.port)

    web.run_app(
        create_app(static_dir=args.static_dir),
        host=args.host,
        port=args.port,
        ssl_context=ssl_context,
        print=None,
    )


if __name__ == "__main__":
    main()
