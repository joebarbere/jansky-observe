"""Tests for the MCP surface — in-memory FastMCP client against a seeded app."""

from __future__ import annotations

import asyncio
import contextlib
import json
import threading
from collections.abc import Callable, Iterator
from typing import Any

import zmq
from fastmcp import Client
from jansky.signals import rng
from sqlmodel import Session

from jansky_observe import synthetic
from jansky_observe.capture.dsp import welch_psd_db
from jansky_observe.capture.writer import NpzCaptureWriter
from jansky_observe.config import Settings
from jansky_observe.db import init_db
from jansky_observe.frames import SpectralFrame
from jansky_observe.mcp import build_mcp
from jansky_observe.models import Capture
from jansky_observe.server.app import create_app


def _app(tmp_path):
    engine = init_db(tmp_path)
    return create_app(Settings(data_dir=str(tmp_path)), engine=engine)


def _registered_capture(app, tmp_path) -> int:
    """Write a synthetic fake-HI .npz and register it as a Capture row."""
    center_hz, rate_hz, n_fft = 1420.4e6, 3e6, 256
    gen = rng(7)
    writer = NpzCaptureWriter(tmp_path / "captures" / "c.npz", settings={"gain": 15})
    samples = n_fft * 32
    for i in range(8):
        iq = synthetic.hi_iq_chunk(
            samples,
            gen,
            t0_s=i * samples / rate_hz,
            center_freq_hz=center_hz,
            sample_rate_hz=rate_hz,
        )
        writer.add_frame(
            SpectralFrame(
                seq=i,
                timestamp=1_750_000_000.0 + i * 0.5,
                center_freq_hz=center_hz,
                sample_rate_hz=rate_hz,
                power_db=welch_psd_db(iq, rate_hz, n_fft),
            )
        )
    path = writer.close()
    with Session(app.state.engine) as session:
        capture = Capture(
            device="synthetic",
            path=str(path),
            format="npz_spectra",
            size_bytes=path.stat().st_size,
            sdr_settings={"gain": 15},
        )
        session.add(capture)
        session.commit()
        session.refresh(capture)
        assert capture.id is not None
        return capture.id


def _payload(result):
    return json.loads(result.content[0].text)


def test_tool_surface_has_no_forbidden_verbs(tmp_path):
    mcp = build_mcp(_app(tmp_path))

    async def names():
        async with Client(mcp) as client:
            return sorted(tool.name for tool in await client.list_tools())

    tools = asyncio.run(names())
    assert tools == [
        "append_note",
        "build_report",
        "create_observation_draft",
        "export_capture",
        "get_calibration_epochs",
        "get_capture_meta",
        "get_diagnostics",
        "get_hi_badge",
        "get_live_status",
        "get_observation",
        "get_observation_bundle",
        "get_pointing",
        "get_rotator_status",
        "get_spectrum",
        "get_station_identity",
        "get_weather",
        "list_observations",
        "reset_hi_badge",
        "run_classifier",
        "slew_rotator",
        "start_rfi_sweep",
        "tick_checklist_item",
        "whats_up",
    ]
    joined = " ".join(tools)
    assert "bias" not in joined and "delete" not in joined  # structural guardrail


def test_whats_up_and_pointing(tmp_path):
    mcp = build_mcp(_app(tmp_path))

    async def scenario():
        async with Client(mcp) as client:
            up = _payload(await client.call_tool("whats_up", {"window_h": 8}))
            assert len(up) >= 6  # all seeded sources
            assert all("el_deg" in row for row in up)
            first_id = up[0]["source_id"]
            pointing = _payload(await client.call_tool("get_pointing", {"source_id": first_id}))
            assert "az_deg" in pointing

    asyncio.run(scenario())


def test_draft_note_tick_round_trip(tmp_path):
    mcp = build_mcp(_app(tmp_path))

    async def scenario():
        async with Client(mcp) as client:
            draft = _payload(
                await client.call_tool(
                    "create_observation_draft",
                    {
                        "observation_type": "HI pointed — Cygnus region",
                        "source": "Cygnus region HI",
                        "name": "mcp draft",
                    },
                )
            )
            assert draft["status"] == "planned"
            assert draft["checklist"], "checklist template not materialized"
            assert all(not item["checked"] for item in draft["checklist"])
            obs_id = draft["id"]

            noted = _payload(
                await client.call_tool(
                    "append_note", {"observation_id": obs_id, "text": "planned via MCP"}
                )
            )
            assert "planned via MCP" in noted["notes"]

            state_id = draft["checklist"][0]["state_id"]
            ticked = _payload(
                await client.call_tool(
                    "tick_checklist_item", {"observation_id": obs_id, "state_id": state_id}
                )
            )
            assert ticked["checked"] and ticked["checked_by"] == "claude"
            refreshed = _payload(
                await client.call_tool("get_observation", {"observation_id": obs_id})
            )
            ticked_item = next(i for i in refreshed["checklist"] if i["state_id"] == state_id)
            assert ticked_item["checked"] and ticked_item["checked_by"] == "claude"

    asyncio.run(scenario())


def test_get_diagnostics_returns_all_checks(tmp_path):
    mcp = build_mcp(_app(tmp_path))

    async def scenario():
        async with Client(mcp) as client:
            return _payload(await client.call_tool("get_diagnostics", {}))

    bundle = asyncio.run(scenario())
    assert set(bundle["checks"]) == {
        "systemd",
        "usb",
        "daemon",
        "thermals",
        "disk",
        "database",
        "journal",
    }
    # The seeded app has a real engine → the schema check is authoritative.
    assert bundle["checks"]["database"]["status"] == "ok"


def test_draft_unknown_type_names_known_types(tmp_path):
    mcp = build_mcp(_app(tmp_path))

    async def scenario():
        async with Client(mcp) as client:
            result = await client.call_tool(
                "create_observation_draft",
                {"observation_type": "nope", "source": "Sun"},
                raise_on_error=False,
            )
            assert result.is_error
            assert "Sun pointing calibration" in result.content[0].text

    asyncio.run(scenario())


def test_capture_and_classifier_round_trip(tmp_path):
    """get_capture_meta → get_spectrum → run_classifier over one .npz fixture."""
    app = _app(tmp_path)
    capture_id = _registered_capture(app, tmp_path)
    mcp = build_mcp(app)

    async def scenario():
        async with Client(mcp) as client:
            meta = _payload(await client.call_tool("get_capture_meta", {"capture_id": capture_id}))
            assert meta["format"] == "npz_spectra"
            assert meta["sdr_settings"]["gain"] == 15

            spectrum = _payload(await client.call_tool("get_spectrum", {"capture_id": capture_id}))
            assert spectrum["axis_kind"] == "mhz"
            assert len(spectrum["axis"]) == len(spectrum["power_db"]) == 256

            # vlsr needs a linked observation — surfaced as a tool error, not a verdict.
            vlsr = await client.call_tool(
                "get_spectrum", {"capture_id": capture_id, "axis": "vlsr"}, raise_on_error=False
            )
            assert vlsr.is_error

            result = _payload(await client.call_tool("run_classifier", {"capture_id": capture_id}))
            assert result["verdict"] == "detected"
            assert result["name"] == "hline_v1"  # provenance: deterministic classifier only
            assert result["mode"] == "post"
            assert result["params"]["window_source"] == "fixed"

    asyncio.run(scenario())


@contextlib.contextmanager
def _fake_daemon(handler: Callable[[dict[str, Any]], dict[str, Any]]) -> Iterator[str]:
    """A plain ZMQ REP control socket (pattern from test_captures_api.py)."""
    ctx = zmq.Context()
    sock = ctx.socket(zmq.REP)
    sock.bind("tcp://127.0.0.1:*")
    endpoint = sock.getsockopt_string(zmq.LAST_ENDPOINT)
    stop = threading.Event()

    def serve() -> None:
        while not stop.is_set():
            if sock.poll(50, zmq.POLLIN):
                request = json.loads(sock.recv())
                sock.send(json.dumps(handler(request)).encode())

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield endpoint
    finally:
        stop.set()
        thread.join(timeout=2.0)
        sock.close(0)
        ctx.term()


def test_start_rfi_sweep_round_trip(tmp_path):
    """start_rfi_sweep proxies /api/rfi_sweep: daemon reply + registered capture id."""
    engine = init_db(tmp_path)
    csv_path = tmp_path / "captures" / "rfi-20260101-000000Z.csv"
    csv_path.parent.mkdir(parents=True)
    csv_path.write_text(
        "2026-01-01, 00:00:00.000000, 1000000000, 1005000000, 1000000.00, 20, -38.00\n"
    )
    canned = {
        "ok": True,
        "path": str(csv_path),
        "num_sweeps": 20,
        "n_rows": 40,
        "freq_range_hz": [1.0e9, 2.0e9],
        "loudest": [{"freq_hz": 1.176e9, "power_db": -38.0}],
    }
    received: list[dict[str, Any]] = []

    def handler(request: dict[str, Any]) -> dict[str, Any]:
        received.append(request)
        return canned

    with _fake_daemon(handler) as endpoint:
        app = create_app(Settings(data_dir=str(tmp_path), ctl_endpoint=endpoint), engine=engine)
        mcp = build_mcp(app)

        async def scenario():
            async with Client(mcp) as client:
                return _payload(
                    await client.call_tool(
                        "start_rfi_sweep", {"freq_lo_mhz": 1000, "freq_hi_mhz": 2000}
                    )
                )

        body = asyncio.run(scenario())
    assert received == [{"cmd": "rfi_sweep", "freq_lo_mhz": 1000.0, "freq_hi_mhz": 2000.0}]
    assert body["loudest"][0]["power_db"] == -38.0
    capture_id = body["capture_id"]
    with Session(app.state.engine) as session:
        capture = session.get(Capture, capture_id)
        assert capture is not None
        assert capture.device == "hackrf" and capture.format == "hackrf_sweep_csv"
        assert capture.path == str(csv_path)


def test_hi_badge_tools(tmp_path):
    mcp = build_mcp(_app(tmp_path))

    async def scenario():
        async with Client(mcp) as client:
            badge = _payload(await client.call_tool("get_hi_badge", {}))
            assert badge == {"status": "accumulating", "n_frames": 0}
            reset = _payload(await client.call_tool("reset_hi_badge", {}))
            assert reset == {"status": "accumulating", "n_frames": 0}

    asyncio.run(scenario())


def test_get_observation_bundle(tmp_path):
    mcp = build_mcp(_app(tmp_path))

    async def scenario():
        async with Client(mcp) as client:
            draft = _payload(
                await client.call_tool(
                    "create_observation_draft",
                    {
                        "observation_type": "HI pointed — Cygnus region",
                        "source": "Cygnus region HI",
                        "name": "bundle draft",
                    },
                )
            )
            bundle = _payload(
                await client.call_tool("get_observation_bundle", {"observation_id": draft["id"]})
            )
            assert bundle["schema"] == "jansky-observe.observation-bundle/1"
            assert len(bundle["station"]["uuid"]) == 36  # plan 78's per-station key
            assert bundle["observation"]["id"] == draft["id"]
            assert bundle["captures"] == []  # a fresh draft has no captures yet

    asyncio.run(scenario())


def test_rotator_mcp_tools(tmp_path):
    from sqlmodel import select

    from jansky_observe.models import Station

    engine = init_db(tmp_path)
    with Session(engine) as s:
        station = s.exec(select(Station)).one()
        station.rotator_kind = "sim"
        station.el_min_deg = -90.0  # any in-envelope target
        s.add(station)
        s.commit()
    app = create_app(Settings(data_dir=str(tmp_path)), engine=engine)
    mcp = build_mcp(app)

    async def scenario():
        async with Client(mcp) as client:
            status = _payload(await client.call_tool("get_rotator_status", {}))
            assert status["kind"] == "sim" and status["configured"] is True
            slewed = _payload(
                await client.call_tool("slew_rotator", {"az_deg": 120.0, "el_deg": 30.0})
            )
            assert slewed == {"ok": True, "az_deg": 120.0, "el_deg": 30.0}

    asyncio.run(scenario())


def test_slew_rotator_refused_outside_limits(tmp_path):
    from sqlmodel import select

    from jansky_observe.models import Station

    engine = init_db(tmp_path)
    with Session(engine) as s:
        station = s.exec(select(Station)).one()
        station.rotator_kind = "sim"
        station.el_min_deg, station.el_max_deg = 80.0, 90.0  # a tight window
        s.add(station)
        s.commit()
    mcp = build_mcp(create_app(Settings(data_dir=str(tmp_path)), engine=engine))

    async def scenario():
        async with Client(mcp) as client:
            raised = False
            try:
                await client.call_tool("slew_rotator", {"az_deg": 120.0, "el_deg": 10.0})
            except Exception:  # noqa: BLE001 - the 422 surfaces as a tool error
                raised = True
            assert raised, "expected the out-of-limits slew to be refused"

    asyncio.run(scenario())


def test_get_station_identity(tmp_path):
    mcp = build_mcp(_app(tmp_path))

    async def scenario():
        async with Client(mcp) as client:
            identity = _payload(await client.call_tool("get_station_identity", {}))
            assert identity["name"] == "Discovery Dish"
            assert len(identity["uuid"]) == 36  # the stable station key
            assert identity["location"]["name"] == "Home"

    asyncio.run(scenario())


def test_mcp_mounted_on_http_app(tmp_path):
    from fastapi.testclient import TestClient

    app = _app(tmp_path)
    with TestClient(app) as client:
        response = client.get("/mcp/")
        assert response.status_code != 404
