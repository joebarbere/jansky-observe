"""Tests for the FastAPI server tier: routes, Broadcaster, the ZMQ→WS pipe, and the capture API."""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import json
import socket
import struct
import threading
import time
from collections.abc import Callable, Iterator
from typing import Any

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


def test_status_bar_route_returns_payload(tmp_path) -> None:
    # Lifespan gives a real engine (seeded station/location → LST + station chip);
    # the dead ctl endpoint makes the source badge unreachable, deterministically.
    app = create_app(Settings(zmq_endpoint=DEAD_ENDPOINT, data_dir=str(tmp_path)))
    with TestClient(app) as client:
        resp = client.get("/api/status_bar")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"server_time_utc", "lst_hours", "station", "source", "weather", "disk"}
    assert body["station"]["name"] == "Discovery Dish"
    assert 0.0 <= body["lst_hours"] < 24.0
    assert body["source"]["reachable"] is False  # dead ctl endpoint
    assert body["disk"]["status"] in {"ok", "warn", "error", "unavailable"}


def test_diagnostics_route_returns_all_checks(tmp_path) -> None:
    # Lifespan gives the app a real engine (database check → ok); the dead ctl
    # endpoint makes the daemon check deterministically an error. The syscall
    # checks are environment-dependent, so only their presence is asserted.
    app = create_app(Settings(zmq_endpoint=DEAD_ENDPOINT, data_dir=str(tmp_path)))
    with TestClient(app) as client:
        resp = client.get("/api/diagnostics")
    assert resp.status_code == 200
    body = resp.json()
    assert body["version"]
    checks = body["checks"]
    assert set(checks) == {"systemd", "usb", "daemon", "thermals", "disk", "database", "journal"}
    assert all("status" in c for c in checks.values())
    assert checks["database"]["status"] == "ok"
    assert checks["daemon"]["status"] == "error"


def test_dt_filter_emits_localizable_time_element() -> None:
    from datetime import datetime

    from jansky_observe.server.routers import _fmt_dt

    out = str(_fmt_dt(datetime(2026, 7, 12, 14, 5, 0)))
    assert '<time class="ts"' in out
    assert 'datetime="2026-07-12T14:05:00Z"' in out
    assert "2026-07-12 14:05 UTC" in out  # JS-off fallback text
    assert str(_fmt_dt(None)) == "—"


def test_index_has_theme_and_localization_controls() -> None:
    client = TestClient(create_app(Settings(zmq_endpoint=DEAD_ENDPOINT)))
    body = client.get("/").text
    assert 'localStorage.getItem("theme")' in body  # no-flash head script
    assert "/static/ui.js" in body
    assert 'id="theme-toggle"' in body
    assert 'id="time-toggle"' in body


def test_ui_and_theme_assets_served() -> None:
    client = TestClient(create_app(Settings(zmq_endpoint=DEAD_ENDPOINT)))
    ui = client.get("/static/ui.js")
    assert ui.status_code == 200
    assert "themechange" in ui.text
    css = client.get("/static/style.css").text
    assert '[data-theme="light"]' in css  # the light palette exists
    assert "prefers-color-scheme: light" in css


def test_spectrum_audio_wired_into_live_view() -> None:
    client = TestClient(create_app(Settings(zmq_endpoint=DEAD_ENDPOINT)))
    body = client.get("/").text
    assert "/static/audio.js" in body
    assert 'id="audio-toggle"' in body
    assert 'id="audio-mode"' in body
    for option in ("receiver", "doppler", "geiger", "drone"):
        assert f'value="{option}"' in body


def test_audio_js_served_with_modes_and_frame_tap() -> None:
    client = TestClient(create_app(Settings(zmq_endpoint=DEAD_ENDPOINT)))
    audio = client.get("/static/audio.js")
    assert audio.status_code == 200
    assert "window.SpectrumAudio" in audio.text
    for mode in ("receiver", "doppler", "geiger", "drone"):
        assert mode in audio.text
    # waterfall.js feeds each PSD frame to the audio engine.
    assert "SpectrumAudio.pushFrame" in client.get("/static/waterfall.js").text


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


def test_index_contains_capture_panel_and_avg_controls() -> None:
    client = TestClient(create_app(Settings(zmq_endpoint=DEAD_ENDPOINT)))
    body = client.get("/").text
    assert 'id="btn-start-npz"' in body
    assert 'id="btn-start-sigmf"' in body
    assert 'id="btn-stop"' in body
    assert 'id="capture-state"' in body
    assert 'id="overrun-badge"' in body
    assert 'id="capture-error"' in body
    assert 'id="btn-reset-avg"' in body
    assert 'id="avg-count"' in body


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


def test_ws_sends_latest_frame_immediately_on_connect(tmp_path) -> None:
    # Entering the lifespan runs init_db (migrate-on-start), so point data_dir at tmp.
    app = create_app(Settings(zmq_endpoint=DEAD_ENDPOINT, data_dir=str(tmp_path)))
    frame = _frame(seq=42)
    with TestClient(app) as client:
        # No clients registered yet: publish just records the latest frame.
        asyncio.run(app.state.broadcaster.publish(frame))
        with client.websocket_connect("/ws/live") as ws:
            data = _receive_bytes_with_timeout(ws, timeout=10.0)
    header, payload = _unpack_ws(data)
    assert header["seq"] == 42
    np.testing.assert_array_equal(payload, frame.power_db)


def test_ws_end_to_end_from_zmq_publisher(tmp_path) -> None:
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
        app = create_app(Settings(zmq_endpoint=endpoint, data_dir=str(tmp_path)))
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


def test_lifespan_starts_and_stops_relay_task_cleanly(tmp_path) -> None:
    app = create_app(Settings(zmq_endpoint=DEAD_ENDPOINT, data_dir=str(tmp_path)))
    with TestClient(app):
        assert not app.state.relay_task.done()
        assert app.state.engine is not None  # init_db ran (migrate-on-start)
    assert app.state.relay_task.done()


# ---- capture API (fake REP daemon) ---------------------------------------------


def _capture_status(**overrides: Any) -> dict[str, Any]:
    """A canned daemon status reply per the control.py schema."""
    reply: dict[str, Any] = {
        "ok": True,
        "capturing": False,
        "format": None,
        "path": None,
        "bytes_written": 0,
        "elapsed_s": 0.0,
        "rate_bytes_per_s": 0.0,
        "disk_free_bytes": 50_000_000_000,
        "source": "synthetic",
    }
    reply.update(overrides)
    return reply


@contextlib.contextmanager
def _fake_daemon(
    handler: Callable[[dict[str, Any]], dict[str, Any]],
) -> Iterator[tuple[str, list[dict[str, Any]]]]:
    """A plain ZMQ REP socket answering control requests with ``handler(request)``.

    Yields ``(endpoint, requests_seen)``.
    """
    ctx = zmq.Context()
    sock = ctx.socket(zmq.REP)
    sock.bind("tcp://127.0.0.1:*")
    endpoint = sock.getsockopt_string(zmq.LAST_ENDPOINT)
    requests_seen: list[dict[str, Any]] = []
    stop = threading.Event()

    def serve() -> None:
        while not stop.is_set():
            if sock.poll(50, zmq.POLLIN):
                request = json.loads(sock.recv())
                requests_seen.append(request)
                sock.send(json.dumps(handler(request)).encode())

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield endpoint, requests_seen
    finally:
        stop.set()
        thread.join(timeout=2.0)
        sock.close(0)
        ctx.term()


def _free_tcp_endpoint() -> str:
    """A localhost TCP endpoint with nothing listening on it."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return f"tcp://127.0.0.1:{s.getsockname()[1]}"


def test_capture_status_idle_adds_disk_conveniences() -> None:
    with _fake_daemon(lambda req: _capture_status()) as (endpoint, seen):
        app = create_app(Settings(zmq_endpoint=DEAD_ENDPOINT, ctl_endpoint=endpoint))
        resp = TestClient(app).get("/api/capture/status")
    assert resp.status_code == 200
    body = resp.json()
    assert seen == [{"cmd": "status"}]
    assert body["capturing"] is False
    assert body["disk_free_gb"] == pytest.approx(50.0)
    assert body["hours_to_full"] is None  # idle: no write rate to project from
    assert body["projected_gb_per_hour"] == {"npz": None, "sigmf": None}  # no frames yet


def test_capture_status_projects_rates_from_latest_frame() -> None:
    with _fake_daemon(lambda req: _capture_status()) as (endpoint, _):
        app = create_app(Settings(zmq_endpoint=DEAD_ENDPOINT, ctl_endpoint=endpoint))

        async def feed() -> None:
            for seq in range(1, 4):  # _frame timestamps are 1 s apart => fps = 1.0
                await app.state.broadcaster.publish(_frame(seq=seq))

        asyncio.run(feed())
        resp = TestClient(app).get("/api/capture/status")
    assert resp.status_code == 200
    proj = resp.json()["projected_gb_per_hour"]
    # sigmf: 3 MSPS x 4 B/s (ci16_le); npz: 64 bins x 4 B x 1 fps.
    assert proj["sigmf"] == pytest.approx(3e6 * 4 * 3600 / 1e9)
    assert proj["npz"] == pytest.approx(64 * 4 * 1.0 * 3600 / 1e9)


def test_capture_status_while_capturing_reports_hours_to_full() -> None:
    status = _capture_status(
        capturing=True,
        format="sigmf",
        path="/data/captures/first-light.sigmf-data",
        bytes_written=1_200_000_000,
        elapsed_s=100.0,
        rate_bytes_per_s=12_000_000.0,
        disk_free_bytes=43_200_000_000,
        overrun=True,
    )
    with _fake_daemon(lambda req: status) as (endpoint, _):
        app = create_app(Settings(zmq_endpoint=DEAD_ENDPOINT, ctl_endpoint=endpoint))
        resp = TestClient(app).get("/api/capture/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["capturing"] is True
    assert body["hours_to_full"] == pytest.approx(1.0)  # 43.2 GB free / 12 MB/s
    assert body["overrun"] is True  # passed through untouched


def test_capture_start_round_trip() -> None:
    def handler(request: dict[str, Any]) -> dict[str, Any]:
        return _capture_status(capturing=True, format=request["format"], path="/data/c.npz")

    with _fake_daemon(handler) as (endpoint, seen):
        app = create_app(Settings(zmq_endpoint=DEAD_ENDPOINT, ctl_endpoint=endpoint))
        resp = TestClient(app).post("/api/capture/start", json={"format": "npz"})
    assert resp.status_code == 200
    assert seen == [{"cmd": "start_capture", "format": "npz"}]
    body = resp.json()
    assert body["capturing"] is True
    assert body["format"] == "npz"
    assert "disk_free_gb" in body  # start replies embed status + conveniences


def test_capture_start_rejects_bad_format_with_422() -> None:
    # Validation happens before any daemon contact, so no fake daemon needed.
    app = create_app(Settings(zmq_endpoint=DEAD_ENDPOINT, ctl_endpoint=DEAD_ENDPOINT))
    client = TestClient(app)
    assert client.post("/api/capture/start", json={"format": "wav"}).status_code == 422
    assert client.post("/api/capture/start", json={}).status_code == 422


def test_capture_start_refusal_maps_to_409() -> None:
    refusal = {"ok": False, "error": "already capturing"}
    with _fake_daemon(lambda req: refusal) as (endpoint, _):
        app = create_app(Settings(zmq_endpoint=DEAD_ENDPOINT, ctl_endpoint=endpoint))
        resp = TestClient(app).post("/api/capture/start", json={"format": "sigmf"})
    assert resp.status_code == 409
    assert "already capturing" in resp.json()["detail"]


def test_capture_stop_round_trip() -> None:
    with _fake_daemon(lambda req: _capture_status()) as (endpoint, seen):
        app = create_app(Settings(zmq_endpoint=DEAD_ENDPOINT, ctl_endpoint=endpoint))
        resp = TestClient(app).post("/api/capture/stop")
    assert resp.status_code == 200
    assert seen == [{"cmd": "stop_capture"}]
    assert resp.json()["capturing"] is False


def test_capture_daemon_unreachable_maps_to_503() -> None:
    # Nothing bound at the ctl endpoint: ctl_request times out (~2 s) → 503.
    app = create_app(Settings(zmq_endpoint=DEAD_ENDPOINT, ctl_endpoint=_free_tcp_endpoint()))
    resp = TestClient(app).get("/api/capture/status")
    assert resp.status_code == 503
    assert "did not reply" in resp.json()["detail"]
