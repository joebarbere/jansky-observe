"""The FastAPI application: routes, WebSocket fan-out, the ZMQ relay, and the capture API.

Data path: the capture daemon PUBlishes ``[JSON header, float32 payload]``
multipart messages; a lifespan task SUBscribes, decodes each message via
:func:`jansky_observe.frames.decode_zmq`, and hands the frame to a
:class:`Broadcaster`, which re-packs it once (:func:`~jansky_observe.frames.pack_ws`)
and pushes the bytes onto every connected browser's bounded queue. Slow
clients drop their oldest frames rather than stalling the fan-out.

Control path: ``/api/capture/*`` routes are thin JSON proxies over
:func:`jansky_observe.control.ctl_request` (the daemon's REQ/REP control
channel). ``ctl_request`` is synchronous with a ~2 s timeout, so it runs in a
worker thread (:func:`asyncio.to_thread`) to keep the event loop free. Status
replies gain server-computed conveniences: free disk in GB, hours until the
disk fills at the current write rate, and the projected GB/h of each capture
format at the current stream parameters.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import deque
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, Literal

import zmq
import zmq.asyncio
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy import Engine

from jansky_observe import __version__
from jansky_observe.config import Settings, settings_from_env
from jansky_observe.control import ctl_request
from jansky_observe.db import init_db
from jansky_observe.frames import SpectralFrame, decode_zmq, pack_ws
from jansky_observe.server.routers import catalog, observations, wizard

__all__ = ["Broadcaster", "CaptureStartBody", "app", "create_app"]

logger = logging.getLogger(__name__)

_PACKAGE_DIR = Path(__file__).resolve().parent
WS_LIVE_PATH = "/ws/live"
DEFAULT_CLIENT_QUEUE_SIZE = 8
FPS_WINDOW = 16
"""Frame-timestamp window used to estimate the live frame rate."""

_GB = 1e9
_SECONDS_PER_HOUR = 3600.0
# ctl_request never raises; these substrings mark its own client-side failure
# replies (timeout / socket error / garbage), i.e. "daemon unreachable" → 503.
# Anything else with ok=false is the daemon refusing a command → 409.
_UNREACHABLE_MARKERS = ("did not reply", "control channel error", "malformed reply")


class CaptureStartBody(BaseModel):
    """Request body for ``POST /api/capture/start``."""

    format: Literal["npz", "sigmf"]


class Broadcaster:
    """Fan spectral frames out to WebSocket clients via bounded per-client queues.

    Each connected client registers a queue; :meth:`publish` stores the frame
    as ``latest`` (so new clients get an immediate first paint) and pushes the
    packed bytes to every queue. Full queues drop their oldest entry first —
    a slow tablet skips frames instead of back-pressuring the relay.

    Parameters
    ----------
    queue_size : int
        Maximum frames buffered per client before drop-oldest kicks in.
    """

    def __init__(self, queue_size: int = DEFAULT_CLIENT_QUEUE_SIZE) -> None:
        self._queue_size = queue_size
        self._queues: set[asyncio.Queue[bytes]] = set()
        self._latest: SpectralFrame | None = None
        self._timestamps: deque[float] = deque(maxlen=FPS_WINDOW)

    @property
    def latest(self) -> SpectralFrame | None:
        """The most recently published frame, or ``None`` before first frame."""
        return self._latest

    @property
    def fps(self) -> float | None:
        """Estimated frame rate from recent frame-header timestamps, or ``None``."""
        if len(self._timestamps) < 2:
            return None
        span = self._timestamps[-1] - self._timestamps[0]
        if span <= 0:
            return None
        return (len(self._timestamps) - 1) / span

    @property
    def client_count(self) -> int:
        """Number of currently registered client queues."""
        return len(self._queues)

    def register(self) -> asyncio.Queue[bytes]:
        """Create and register a new client queue."""
        queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=self._queue_size)
        self._queues.add(queue)
        return queue

    def unregister(self, queue: asyncio.Queue[bytes]) -> None:
        """Remove a client queue; safe to call twice."""
        self._queues.discard(queue)

    async def publish(self, frame: SpectralFrame) -> None:
        """Store ``frame`` as latest and push its packed bytes to every client."""
        self._latest = frame
        self._timestamps.append(frame.timestamp)
        if not self._queues:
            return
        data = pack_ws(frame)
        for queue in self._queues:
            self._put_drop_oldest(queue, data)

    @staticmethod
    def _put_drop_oldest(queue: asyncio.Queue[bytes], data: bytes) -> None:
        """Enqueue without blocking, evicting the oldest entry if full."""
        while True:
            try:
                queue.put_nowait(data)
                return
            except asyncio.QueueFull:
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()


def _projected_gb_per_hour(
    latest: SpectralFrame | None, fps: float | None
) -> dict[str, float | None]:
    """Projected capture rates (GB/h) per format at the current stream parameters.

    SigMF stores raw IQ at ``sample_rate × 4`` bytes/s (``ci16_le``, the
    Airspy's native INT16 — the plan's ~43 GB/h at 3 MSPS); ``.npz`` stores
    reduced spectra at ``n_fft × 4 × fps`` bytes/s (float32 rows).
    Values are ``None`` when the inputs are unknown (no frame seen yet, or no
    frame-rate estimate).

    Parameters
    ----------
    latest : SpectralFrame or None
        The most recent frame from the daemon, if any.
    fps : float or None
        Estimated frame rate.

    Returns
    -------
    dict
        ``{"npz": float | None, "sigmf": float | None}``.
    """
    if latest is None:
        return {"npz": None, "sigmf": None}
    sigmf = latest.sample_rate_hz * 4.0 * _SECONDS_PER_HOUR / _GB
    npz = None if fps is None else latest.power_db.size * 4.0 * fps * _SECONDS_PER_HOUR / _GB
    return {"npz": npz, "sigmf": sigmf}


def _raise_for_ctl_error(reply: dict[str, Any]) -> None:
    """Map an ``{"ok": false}`` control reply onto an HTTP error.

    503 when :func:`ctl_request` itself failed (daemon unreachable / timed
    out), 409 when the daemon replied but refused the command.
    """
    error = str(reply.get("error", "unknown control error"))
    if any(marker in error for marker in _UNREACHABLE_MARKERS):
        raise HTTPException(status_code=503, detail=error)
    raise HTTPException(status_code=409, detail=error)


async def _zmq_relay(endpoint: str, broadcaster: Broadcaster) -> None:
    """SUBscribe to the capture daemon and publish decoded frames.

    ZeroMQ connects lazily, so a daemon that is not running yet is fine: the
    socket sits in ``recv`` (no busy loop) and frames flow whenever the
    publisher appears. Malformed messages are logged and dropped.
    """
    ctx = zmq.asyncio.Context()
    sock = ctx.socket(zmq.SUB)
    sock.setsockopt(zmq.SUBSCRIBE, b"")
    sock.setsockopt(zmq.LINGER, 0)
    try:
        sock.connect(endpoint)
        logger.info("ZMQ relay subscribing to %s", endpoint)
        while True:
            parts = await sock.recv_multipart()
            try:
                frame = decode_zmq(parts)
            except (ValueError, KeyError, TypeError) as exc:
                logger.warning("dropping malformed frame: %s", exc)
                continue
            await broadcaster.publish(frame)
    except zmq.ZMQError as exc:
        logger.error("ZMQ relay for %s stopped: %s", endpoint, exc)
    finally:
        sock.close(0)
        ctx.term()


@contextlib.asynccontextmanager
async def _lifespan(application: FastAPI) -> AsyncIterator[None]:
    """Open/migrate the database and run the ZMQ relay for the app's lifetime.

    ``init_db`` is the migrate-on-start promise (plan §9, M2): the SQLite
    schema is walked forward every server start. Tests inject a ready engine
    via :func:`create_app`, in which case migration is skipped here.
    """
    settings: Settings = application.state.settings
    broadcaster: Broadcaster = application.state.broadcaster
    if application.state.engine is None:
        application.state.engine = init_db(settings.data_dir)
    task = asyncio.create_task(_zmq_relay(settings.zmq_endpoint, broadcaster))
    application.state.relay_task = task
    # FastMCP's HTTP transport runs a session manager inside the sub-app's own
    # lifespan — it must be entered here or /mcp requests 500.
    mcp_app = application.state.mcp_app
    try:
        async with mcp_app.router.lifespan_context(mcp_app):
            yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def create_app(settings: Settings | None = None, engine: Engine | None = None) -> FastAPI:
    """Build the FastAPI application.

    Parameters
    ----------
    settings : Settings, optional
        Runtime settings; defaults to :func:`jansky_observe.config.settings_from_env`.
    engine : Engine, optional
        A ready (already migrated) database engine — used by tests. When
        omitted, the lifespan opens and migrates ``<data_dir>`` on start.

    Returns
    -------
    FastAPI
        The application, with ``settings``, ``broadcaster``, and ``engine``
        on ``app.state``.
    """
    if settings is None:
        settings = settings_from_env()

    templates = Jinja2Templates(directory=str(_PACKAGE_DIR / "templates"))
    application = FastAPI(title="jansky-observe", version=__version__, lifespan=_lifespan)
    application.state.settings = settings
    application.state.broadcaster = Broadcaster()
    application.state.engine = engine
    application.mount("/static", StaticFiles(directory=str(_PACKAGE_DIR / "static")), name="static")
    application.include_router(observations.router)
    application.include_router(catalog.router)
    application.include_router(wizard.router)
    # The MCP surface (plan §12.4): Claude as a console peer of the browser UI.
    from jansky_observe.mcp import mount_mcp

    application.state.mcp_app = mount_mcp(application)

    @application.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> HTMLResponse:
        """The single live-view page."""
        return templates.TemplateResponse(
            request, "index.html", {"version": __version__, "ws_path": WS_LIVE_PATH}
        )

    @application.get("/healthz")
    async def healthz() -> dict[str, str]:
        """Liveness probe used by deploy/install.sh."""
        return {"status": "ok", "version": __version__}

    async def _ctl(request: dict[str, Any]) -> dict[str, Any]:
        """Send one control request off-loop; raise HTTPException on any failure."""
        reply = await asyncio.to_thread(ctl_request, settings.ctl_endpoint, request)
        if not reply.get("ok"):
            _raise_for_ctl_error(reply)
        return reply

    def _with_disk_conveniences(reply: dict[str, Any]) -> dict[str, Any]:
        """Add ``disk_free_gb``, ``hours_to_full``, and per-format projections."""
        broadcaster: Broadcaster = application.state.broadcaster
        disk_free = float(reply.get("disk_free_bytes", 0))
        rate = float(reply.get("rate_bytes_per_s", 0.0))
        reply["disk_free_gb"] = disk_free / _GB
        reply["hours_to_full"] = (
            disk_free / rate / _SECONDS_PER_HOUR if reply.get("capturing") and rate > 0 else None
        )
        reply["projected_gb_per_hour"] = _projected_gb_per_hour(broadcaster.latest, broadcaster.fps)
        return reply

    @application.get("/api/capture/status")
    async def capture_status() -> dict[str, Any]:
        """The daemon's status reply plus disk/projection conveniences."""
        return _with_disk_conveniences(await _ctl({"cmd": "status"}))

    @application.post("/api/capture/start")
    async def capture_start(body: CaptureStartBody) -> dict[str, Any]:
        """Start a capture in the requested format; 409 if the daemon refuses."""
        return _with_disk_conveniences(await _ctl({"cmd": "start_capture", "format": body.format}))

    @application.post("/api/capture/stop")
    async def capture_stop() -> dict[str, Any]:
        """Stop the running capture; 409 if the daemon refuses."""
        return _with_disk_conveniences(await _ctl({"cmd": "stop_capture"}))

    @application.websocket(WS_LIVE_PATH)
    async def ws_live(websocket: WebSocket) -> None:
        """Stream packed spectral frames to one browser until it disconnects."""
        broadcaster: Broadcaster = websocket.app.state.broadcaster
        await websocket.accept()
        queue = broadcaster.register()
        try:
            latest = broadcaster.latest
            if latest is not None:
                await websocket.send_bytes(pack_ws(latest))
            while True:
                await websocket.send_bytes(await queue.get())
        except (WebSocketDisconnect, RuntimeError):
            pass  # client went away; RuntimeError covers send-after-close races
        finally:
            broadcaster.unregister(queue)

    return application


app = create_app()
"""Module-level app for ``uvicorn jansky_observe.server.app:app``."""
