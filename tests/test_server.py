"""Tests for the FastAPI server tier: routes, Broadcaster, and the ZMQ→WS pipe."""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import struct
import threading
import time

import numpy as np
import pytest
import zmq
from fastapi.testclient import TestClient

from jansky_observe import __version__, frames
from jansky_observe.config import Settings
from jansky_observe.server.app import Broadcaster, create_app

# An endpoint with no publisher behind it: ZMQ connects lazily, so this is a
# safe "daemon not running" stand-in that must not break startup.
DEAD_ENDPOINT = "tcp://127.0.0.1:1"


def _frame(seq: int = 1, n: int = 64) -> frames.SpectralFrame:
    rng = np.random.default_rng(seq)
    power = rng.normal(-90.0, 2.0, n).astype(np.float32)
    return frames.SpectralFrame(
        seq=seq,
        timestamp=1_752_000_000.0 + seq,
        center_freq_hz=1_420_400_000.0,
        sample_rate_hz=3_000_000.0,
        power_db=power,
    )


def _unpack_ws(data: bytes) -> tuple[dict, np.ndarray]:
    (hlen,) = struct.unpack_from("<I", data, 0)
    header = json.loads(data[4 : 4 + hlen].decode())
    payload = np.frombuffer(data[4 + hlen :], dtype="<f4")
    return header, payload


# ---- plain HTTP routes (no lifespan needed) -----------------------------------


def test_healthz_returns_ok_and_version() -> None:
    client = TestClient(create_app(Settings(zmq_endpoint=DEAD_ENDPOINT)))
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "version": __version__}


def test_index_renders_live_page() -> None:
    client = TestClient(create_app(Settings(zmq_endpoint=DEAD_ENDPOINT)))
    resp = client.get("/")
    assert resp.status_code == 200
    body = resp.text
    assert 'id="spectrum"' in body
    assert 'id="waterfall"' in body
    assert "/static/waterfall.js" in body
    assert "/static/style.css" in body
    assert "/ws/live" in body
    assert __version__ in body


def test_static_files_served() -> None:
    client = TestClient(create_app(Settings(zmq_endpoint=DEAD_ENDPOINT)))
    js = client.get("/static/waterfall.js")
    assert js.status_code == 200
    assert "javascript" in js.headers["content-type"]
    assert "pack_ws" in js.text
    css = client.get("/static/style.css")
    assert css.status_code == 200


# ---- Broadcaster unit tests -----------------------------------------------------


def test_broadcaster_publish_delivers_packed_frame() -> None:
    async def run() -> None:
        broadcaster = Broadcaster()
        queue = broadcaster.register()
        frame = _frame(seq=3)
        await broadcaster.publish(frame)
        assert queue.get_nowait() == frames.pack_ws(frame)

    asyncio.run(run())


def test_broadcaster_retains_latest_frame() -> None:
    async def run() -> None:
        broadcaster = Broadcaster()
        assert broadcaster.latest is None
        await broadcaster.publish(_frame(seq=1))
        await broadcaster.publish(_frame(seq=2))
        assert broadcaster.latest is not None
        assert broadcaster.latest.seq == 2

    asyncio.run(run())


def test_broadcaster_slow_client_drops_oldest_without_blocking() -> None:
    async def run() -> None:
        broadcaster = Broadcaster(queue_size=2)
        queue = broadcaster.register()
        for seq in range(1, 6):
            # Must never block even though nobody drains the queue.
            await asyncio.wait_for(broadcaster.publish(_frame(seq=seq)), timeout=1.0)
        assert queue.qsize() == 2
        header, _ = _unpack_ws(queue.get_nowait())
        assert header["seq"] == 4  # oldest surviving frame
        header, _ = _unpack_ws(queue.get_nowait())
        assert header["seq"] == 5  # newest frame

    asyncio.run(run())


def test_broadcaster_unregister_stops_delivery() -> None:
    async def run() -> None:
        broadcaster = Broadcaster()
        queue = broadcaster.register()
        broadcaster.unregister(queue)
        broadcaster.unregister(queue)  # idempotent
        await broadcaster.publish(_frame(seq=1))
        assert queue.qsize() == 0
        assert broadcaster.client_count == 0

    asyncio.run(run())


# ---- WebSocket ---------------------------------------------------------------


def _receive_bytes_with_timeout(ws, timeout: float) -> bytes:
    """Guard the blocking TestClient receive so a broken pipe fails, not hangs."""
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        return executor.submit(ws.receive_bytes).result(timeout=timeout)
    except concurrent.futures.TimeoutError:
        pytest.fail(f"no WebSocket frame within {timeout}s")
    finally:
        executor.shutdown(wait=False)


def test_ws_sends_latest_frame_immediately_on_connect() -> None:
    app = create_app(Settings(zmq_endpoint=DEAD_ENDPOINT))
    frame = _frame(seq=42)
    with TestClient(app) as client:
        # No clients registered yet: publish just records the latest frame.
        asyncio.run(app.state.broadcaster.publish(frame))
        with client.websocket_connect("/ws/live") as ws:
            data = _receive_bytes_with_timeout(ws, timeout=10.0)
    header, payload = _unpack_ws(data)
    assert header["seq"] == 42
    np.testing.assert_array_equal(payload, frame.power_db)


def test_ws_end_to_end_from_zmq_publisher() -> None:
    ctx = zmq.Context()
    pub = ctx.socket(zmq.PUB)
    pub.bind("tcp://127.0.0.1:*")
    endpoint = pub.getsockopt_string(zmq.LAST_ENDPOINT)
    frame = _frame(seq=7, n=128)
    parts = frames.encode_zmq(frame)
    stop = threading.Event()

    def pump() -> None:
        # PUB/SUB slow-joiner: keep publishing until the subscriber sees one.
        while not stop.is_set():
            pub.send_multipart(parts)
            time.sleep(0.05)

    pumper = threading.Thread(target=pump, daemon=True)
    try:
        app = create_app(Settings(zmq_endpoint=endpoint))
        with TestClient(app) as client, client.websocket_connect("/ws/live") as ws:
            pumper.start()
            data = _receive_bytes_with_timeout(ws, timeout=15.0)
    finally:
        stop.set()
        if pumper.is_alive():
            pumper.join(timeout=2.0)
        pub.close(0)
        ctx.term()

    header, payload = _unpack_ws(data)
    assert header["v"] == frames.SCHEMA_VERSION
    assert header["seq"] == 7
    assert header["n_fft"] == 128
    assert header["center_freq_hz"] == frame.center_freq_hz
    assert header["sample_rate_hz"] == frame.sample_rate_hz
    assert header["timestamp"] == frame.timestamp
    np.testing.assert_array_equal(payload, frame.power_db)


def test_lifespan_starts_and_stops_relay_task_cleanly() -> None:
    app = create_app(Settings(zmq_endpoint=DEAD_ENDPOINT))
    with TestClient(app):
        assert not app.state.relay_task.done()
    assert app.state.relay_task.done()
