"""The FastAPI application: routes, WebSocket fan-out, and the ZMQ relay.

Data path: the capture daemon PUBlishes ``[JSON header, float32 payload]``
multipart messages; a lifespan task SUBscribes, decodes each message via
:func:`jansky_observe.frames.decode_zmq`, and hands the frame to a
:class:`Broadcaster`, which re-packs it once (:func:`~jansky_observe.frames.pack_ws`)
and pushes the bytes onto every connected browser's bounded queue. Slow
clients drop their oldest frames rather than stalling the fan-out.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from pathlib import Path

import zmq
import zmq.asyncio
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from jansky_observe import __version__
from jansky_observe.config import Settings, settings_from_env
from jansky_observe.frames import SpectralFrame, decode_zmq, pack_ws

__all__ = ["Broadcaster", "app", "create_app"]

logger = logging.getLogger(__name__)

_PACKAGE_DIR = Path(__file__).resolve().parent
WS_LIVE_PATH = "/ws/live"
DEFAULT_CLIENT_QUEUE_SIZE = 8


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

    @property
    def latest(self) -> SpectralFrame | None:
        """The most recently published frame, or ``None`` before first frame."""
        return self._latest

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
    """Run the ZMQ relay task for the app's lifetime; cancel it cleanly on shutdown."""
    settings: Settings = application.state.settings
    broadcaster: Broadcaster = application.state.broadcaster
    task = asyncio.create_task(_zmq_relay(settings.zmq_endpoint, broadcaster))
    application.state.relay_task = task
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the FastAPI application.

    Parameters
    ----------
    settings : Settings, optional
        Runtime settings; defaults to :func:`jansky_observe.config.settings_from_env`.

    Returns
    -------
    FastAPI
        The application, with ``settings`` and ``broadcaster`` on ``app.state``.
    """
    if settings is None:
        settings = settings_from_env()

    templates = Jinja2Templates(directory=str(_PACKAGE_DIR / "templates"))
    application = FastAPI(title="jansky-observe", version=__version__, lifespan=_lifespan)
    application.state.settings = settings
    application.state.broadcaster = Broadcaster()
    application.mount("/static", StaticFiles(directory=str(_PACKAGE_DIR / "static")), name="static")

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
