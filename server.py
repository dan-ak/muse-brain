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
from collections import deque
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

# Window over which each seat's arrival rate is measured.
RATE_WINDOW_S = 2.0

# How often buffered recording data is pushed to the OS.
FLUSH_EVERY_S = 2.0

# Browsers throttle timers in hidden tabs to roughly 1 Hz, so a phone that
# locks its screen silently drops from 30 Hz to 1 Hz while still looking
# connected. Anything this slow is reported as throttled rather than healthy.
THROTTLED_BELOW_HZ = 5.0

# Where a plain-HTTP request gets bounced to HTTPS. Browsers do not upgrade
# bare addresses, and people type hostnames without a scheme, so without this
# the first thing a new device sees is a connection refused.
DEFAULT_REDIRECT_PORT = 80

HUB = web.AppKey("hub")
STATIC_DIR = web.AppKey("static_dir", Path)
BROADCAST_TASK = web.AppKey("broadcast_task")
REDIRECT_CONFIG = web.AppKey("redirect_config")
REDIRECT_RUNNER = web.AppKey("redirect_runner")

# Outcomes of handling one websocket frame.
TELEMETRY = "telemetry"
CALIBRATED = "calibrated"
IGNORED = "ignored"

# Synthetic addresses for SessionRecorder. These are not OSC addresses off a
# headset; see the FIXED table in session_recorder.py.
ADDR_TELEMETRY = "/pwa/telemetry"
ADDR_CALIBRATION = "/pwa/calibration"
ADDR_SEAT = "/pwa/seat"

# Raw streams the client may include in a batch, with the nominal sample rate
# used to space samples within that batch and the expected values per sample.
# A stream absent from a batch is normal: the MU-02 has no PPG hardware.
RAW_STREAMS = {
    "eeg": {"address": "/pwa/eeg", "rate": 256.0, "width": 4},
    "ppg": {"address": "/pwa/ppg", "rate": 64.0, "width": 3},
    "acc": {"address": "/pwa/acc", "rate": 52.0, "width": 3},
    "gyro": {"address": "/pwa/gyro", "rate": 52.0, "width": 3},
}

# Band powers arrive as one row per channel and carry their own channel index,
# so they are grouped into DSP ticks rather than spaced per row.
ADDR_BANDS = "/pwa/bands"
BANDS_WIDTH = 6  # channel + five band powers
BANDS_HZ = 4.0  # the client's DSP interval

# Marks a discontinuity where the client reported losing samples, so a dropout
# is visible in the recording itself rather than only in a live counter.
ADDR_GAP = "/pwa/gap"

RAW = "raw"


def _is_finite_number(value):
    """True for a real, finite number — and notably False for bool and None.

    JSON.stringify turns NaN and Infinity into null, and powerByBand can produce
    either, so without this check nulls and strings were written straight into
    numeric CSV columns while meta.json still reported zero errors.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return value == value and value not in (float("inf"), float("-inf"))


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
        # Injectable so staleness and rate are testable without waiting in real time.
        self._clock = clock or time.monotonic
        self._last_seen = {sid: None for sid in self.seat_ids}
        self._arrivals = {sid: deque() for sid in self.seat_ids}
        # Times a client buffer filled and discarded samples, per seat, this session.
        # The buffer drops incoming samples when full, so the count of lost
        # samples is unknowable — only that loss happened.
        self._overflows = {sid: 0 for sid in self.seat_ids}
        # Last timestamp written per (seat, stream), so a delayed batch cannot be
        # back-dated to before rows already on disk.
        self._last_sample_t = {}

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

        if data.get("type") == RAW:
            return self._handle_raw(seat_id, data)

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
        self._note_arrival(seat_id)
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

    @property
    def player_sockets(self):
        return tuple(self._sockets.values())

    def recording_message(self):
        """Told to players so they only stream raw while a session is running.

        Raw capture costs phone battery and CPU, and most of the time nobody is
        recording, so clients stay quiet until asked.
        """
        return {"type": "recording", "active": self.recording, "label": self._label}

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

        # Starting again while active is a no-op in SessionRecorder, which keeps
        # its original directory. Mutating label and counters here anyway made
        # the response, the announcement and meta.json disagree, and zeroed the
        # loss counts for a session that had already logged some.
        if self._recorder.active:
            return self._recorder.session_dir

        self._label = label
        # Overflow counts describe one session, not the process lifetime.
        self._overflows = {sid: 0 for sid in self.seat_ids}
        self._last_sample_t = {}
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

    def _record(self, address, values, t=None):
        """Best-effort recording. Never let it break a player connection."""
        if self._recorder is None or not self._recorder.active:
            return
        try:
            self._recorder.record(address, values, t=t)
        except Exception:
            logger.exception("recording %s failed", address)

    def flush_recorder(self):
        if self._recorder is None or not self._recorder.active:
            return
        try:
            self._recorder.flush()
        except Exception:
            logger.exception("flushing the recording failed")

    # -- raw capture ---------------------------------------------------------

    def _handle_raw(self, seat_id, data):
        """Record one batch of raw samples.

        Batches carry no per-sample timestamps, so each is back-dated from its
        arrival: N samples at rate R ending now means sample i sits at
        now - (N-1-i)/R. Spacing inside a batch is then exact, and the error
        between batches is network latency — a few ms against EEG's 3.9 ms
        sample interval.
        """
        if not self.recording:
            # Nothing to write to. Not an error: a client may still be finishing
            # a batch as a session stops.
            return RAW

        arrival = self._recorder_now()
        lost = self._note_overflows(seat_id, data.get("overflows"))

        for name, spec in RAW_STREAMS.items():
            samples = data.get(name)
            if not isinstance(samples, list) or not samples:
                continue
            self._record_sampled(seat_id, samples, spec, arrival, lost)

        bands = data.get("bands")
        if isinstance(bands, list):
            self._record_bands(seat_id, bands, arrival)

        return RAW

    def _note_overflows(self, seat_id, overflows):
        """Accumulate reported buffer overflows. Returns whether any were new.

        int() raises OverflowError on a float infinity, which json.loads accepts
        as a bare Infinity literal — and OverflowError is not a subclass of
        ValueError, so catching only those let it escape handle_message and tear
        down the socket.
        """
        if not isinstance(overflows, dict):
            return False

        total = 0
        for count in overflows.values():
            try:
                total += max(0, int(count))
            except (TypeError, ValueError, OverflowError):
                continue

        self._overflows[seat_id] += total
        return total > 0

    def _record_sampled(self, seat_id, samples, spec, arrival, lost=False):
        """Write one batch of samples, back-dated from arrival.

        Timestamps are clamped to stay after the last row written for this
        stream. Without that, a batch delayed by wifi power-save is back-dated
        to before the batch already on disk, leaving a non-monotonic time column
        that silently breaks anything assuming ordering.
        """
        rows = [
            sample
            for sample in samples
            if isinstance(sample, list)
            and len(sample) == spec["width"]
            and all(_is_finite_number(v) for v in sample)
        ]
        if not rows:
            return

        step = 1.0 / spec["rate"]
        first = arrival - (len(rows) - 1) * step

        key = (seat_id, spec["address"])
        previous = self._last_sample_t.get(key)
        if previous is not None and first <= previous:
            first = previous + step

        for index, sample in enumerate(rows):
            t = first + index * step
            self._record(spec["address"], [seat_id] + list(sample), t=t)
        self._last_sample_t[key] = first + (len(rows) - 1) * step

        # A batch that followed reported loss is not contiguous with the one
        # before it, so mark the discontinuity rather than leaving the rows
        # looking continuous. Without this the only trace of a dropout is a
        # counter in memory that never reaches disk.
        if lost:
            self._record(ADDR_GAP, [seat_id, spec["address"], f"{first:.6f}"])

    def _record_bands(self, seat_id, bands, arrival):
        """Write band-power rows, spacing successive DSP windows apart.

        Rows arrive four at a time (one per channel) per 250 ms DSP tick. A
        delayed send carries several ticks, and stamping them all with `arrival`
        collapsed distinct windows onto one timestamp — so anything pivoting on
        (t, channel) silently kept one window and discarded the rest.
        """
        valid = [
            row
            for row in bands
            if isinstance(row, list)
            and len(row) == BANDS_WIDTH
            and all(_is_finite_number(v) for v in row)
        ]
        if not valid:
            return

        # Group consecutive rows into ticks by watching the channel index restart.
        ticks, current = [], []
        for row in valid:
            if current and row[0] <= current[-1][0]:
                ticks.append(current)
                current = []
            current.append(row)
        if current:
            ticks.append(current)

        step = 1.0 / BANDS_HZ
        first = arrival - (len(ticks) - 1) * step
        key = (seat_id, ADDR_BANDS)
        previous = self._last_sample_t.get(key)
        if previous is not None and first <= previous:
            first = previous + step

        for index, tick in enumerate(ticks):
            t = first + index * step
            for row in tick:
                self._record(ADDR_BANDS, [seat_id] + list(row), t=t)
        self._last_sample_t[key] = first + (len(ticks) - 1) * step

    def _recorder_now(self):
        """Session-relative time, matching what SessionRecorder would stamp."""
        if self._recorder is None:
            return 0.0
        return self._recorder.elapsed()

    def overflows_for(self, seat_id):
        return self._overflows.get(seat_id, 0)

    # -- snapshot ------------------------------------------------------------

    def _note_arrival(self, seat_id):
        now = self._clock()
        arrivals = self._arrivals[seat_id]
        arrivals.append(now)
        cutoff = now - RATE_WINDOW_S
        while arrivals and arrivals[0] < cutoff:
            arrivals.popleft()

    def rate_of(self, seat_id):
        """Frames per second over the recent window.

        Measured from arrival timestamps rather than counted per wall-clock
        second, so it reflects the rate right now instead of averaging across a
        transition — a phone that just locked its screen should read ~1 Hz
        immediately, not drift down over a minute.
        """
        arrivals = self._arrivals[seat_id]
        if len(arrivals) < 2:
            return 0.0
        span = self._clock() - arrivals[0]
        if span <= 0:
            return 0.0
        return len(arrivals) / span

    def is_throttled(self, seat_id):
        """True when a connected seat is streaming far below the intended rate.

        The signature of a backgrounded tab: still connected, still sending,
        but at the ~1 Hz browsers clamp hidden timers to.
        """
        if seat_id not in self._sockets or self.is_stale(seat_id):
            return False
        return self.rate_of(seat_id) < THROTTLED_BELOW_HZ

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
                    rate=round(self.rate_of(sid), 1),
                    throttled=self.is_throttled(sid),
                    overflows=self.overflows_for(sid),
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


async def _announce_recording(hub):
    """Tell every connected player whether a session is running."""
    payload = json.dumps(hub.recording_message())
    for socket in hub.player_sockets:
        # A player that has gone away is the disconnect path's problem, not this
        # one's; failing here must not abort telling everybody else.
        with contextlib.suppress(Exception):
            await socket.send_str(payload)


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
    await _announce_recording(hub)
    return web.json_response({"active": True, "label": label, "dir": str(directory)})


async def handle_session_stop(request):
    hub = request.app[HUB]
    directory = hub.stop_recording()
    logger.info("Recording stopped: %s", directory)
    await _announce_recording(hub)
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

    # Announce on connect as well as on change: a phone joining mid-session
    # would otherwise stay silent until the next start, and its data would be
    # missing from a recording that looked complete.
    with contextlib.suppress(Exception):
        await socket.send_str(json.dumps(hub.recording_message()))

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
    ticks_per_flush = max(1, int(FLUSH_EVERY_S * OBSERVER_HZ))
    tick = 0

    while True:
        await asyncio.sleep(interval)

        # Raw capture writes through a 64 KB buffer, so an unclean shutdown
        # costs whatever is unflushed. Bounding that to a couple of seconds
        # matters for a Pi running off a battery.
        tick += 1
        if tick % ticks_per_flush == 0:
            hub.flush_recorder()

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


def make_redirect_app(https_port=443):
    """A tiny app whose only job is to bounce plain HTTP over to HTTPS.

    Kept separate from the main app so it can be served on its own port and
    tested on its own. A temporary redirect rather than permanent: a permanent
    one gets cached hard by every phone that ever hits it, which is awkward to
    undo on someone else's device in a desert.
    """

    async def redirect(request):
        host = request.host.split(":")[0]
        target = f"https://{host}"
        if https_port != 443:
            target += f":{https_port}"
        target += str(request.rel_url)
        raise web.HTTPFound(target)

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", redirect)
    return app


async def _start_redirect(app):
    config = app.get(REDIRECT_CONFIG)
    if not config:
        return
    host, port, https_port = config

    runner = web.AppRunner(make_redirect_app(https_port))
    await runner.setup()
    try:
        await web.TCPSite(runner, host, port).start()
    except OSError as exc:
        # Losing the convenience redirect is not worth refusing to serve.
        logger.warning("HTTP redirect on port %s unavailable: %s", port, exc)
        await runner.cleanup()
        return
    app[REDIRECT_RUNNER] = runner
    logger.info("Redirecting http://%s:%s to https", host, port)


async def _stop_redirect(app):
    runner = app.get(REDIRECT_RUNNER)
    if runner is not None:
        await runner.cleanup()


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
    app.on_startup.append(_start_redirect)
    app.on_cleanup.append(_stop_broadcast)
    app.on_cleanup.append(_stop_redirect)
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
    parser.add_argument(
        "--redirect-port",
        type=int,
        default=int(os.environ.get("MUSE_REDIRECT_PORT", DEFAULT_REDIRECT_PORT)),
        help="plain-HTTP port that redirects to HTTPS; 0 disables",
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

    app = create_app(static_dir=args.static_dir, hub=hub)

    # Only meaningful when actually serving TLS; without a certificate there is
    # nothing to redirect anyone to.
    if ssl_context is not None and args.redirect_port:
        app[REDIRECT_CONFIG] = (args.host, args.redirect_port, args.port)

    web.run_app(
        app,
        host=args.host,
        port=args.port,
        ssl_context=ssl_context,
        print=None,
    )


if __name__ == "__main__":
    main()
