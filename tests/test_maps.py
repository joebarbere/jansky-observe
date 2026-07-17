"""HI sky maps (roadmap M11): reduction-over-DB, the raster runner, and routes."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlmodel import Session, select

from jansky_observe.astro.lsr import HI_LINE_FREQ_HZ
from jansky_observe.config import Settings
from jansky_observe.db import init_db
from jansky_observe.models import Campaign, Capture, RadioSource, SkyMap, Station
from jansky_observe.server import mapping as server_mapping
from jansky_observe.server.app import create_app
from jansky_observe.server.mapping import (
    MapRunState,
    azel_to_galactic,
    build_sky_map,
    map_cells,
    map_tick,
    next_map_cell,
)
from jansky_observe.server.routers import default_location, default_station

DEAD_ENDPOINT = "tcp://127.0.0.1:1"
CENTER_HZ = HI_LINE_FREQ_HZ
RATE_HZ = 6.0e6


@pytest.fixture()
def engine(tmp_path) -> Engine:
    return init_db(tmp_path / "data")


@pytest.fixture()
def client(engine: Engine, tmp_path) -> TestClient:
    return TestClient(
        create_app(
            Settings(zmq_endpoint=DEAD_ENDPOINT, data_dir=str(tmp_path / "data")), engine=engine
        )
    )


def _write_npz(path: Path, *, bump_db: float = 8.0, n_fft: int = 256, n_frames: int = 3) -> Path:
    """A minimal .npz averaged-spectrum capture with a Gaussian HI line."""
    freq = CENTER_HZ + (np.arange(n_fft) - n_fft / 2) * (RATE_HZ / n_fft)
    line = bump_db * np.exp(-0.5 * ((freq - CENTER_HZ) / 1.2e5) ** 2)
    row = -40.0 + line
    power_db = np.tile(row, (n_frames, 1)).astype(np.float64)
    path.parent.mkdir(parents=True, exist_ok=True)
    np.savez(
        path,
        power_db=power_db,
        center_freq_hz=CENTER_HZ,
        sample_rate_hz=RATE_HZ,
        timestamps=np.arange(n_frames, dtype=np.float64),
        settings=json.dumps({"gain": 15, "source": "synthetic"}),
    )
    return path


def _tag_capture(engine: Engine, sky_map_id: int, az: float, el: float, npz: Path) -> int:
    with Session(engine) as session:
        capture = Capture(
            device="synthetic",
            path=str(npz),
            format="npz_spectra",
            size_bytes=npz.stat().st_size,
            start=datetime(2026, 7, 1, 3, 0, tzinfo=UTC),
            sky_map_id=sky_map_id,
            map_az_deg=az,
            map_el_deg=el,
        )
        session.add(capture)
        session.commit()
        session.refresh(capture)
        assert capture.id is not None
        return capture.id


def _make_map(engine: Engine, **kw: Any) -> int:
    with Session(engine) as session:
        sky_map = SkyMap(name=kw.pop("name", "m"), **kw)
        session.add(sky_map)
        session.commit()
        session.refresh(sky_map)
        assert sky_map.id is not None
        return sky_map.id


# ---- frame projection --------------------------------------------------------------


def test_azel_to_galactic_is_finite() -> None:
    l_deg, b_deg = azel_to_galactic(
        180.0, 45.0, 40.0, -74.0, 30.0, datetime(2026, 7, 1, 3, tzinfo=UTC)
    )
    assert -360.0 <= l_deg <= 360.0
    assert -90.0 <= b_deg <= 90.0


# ---- build (read path) -------------------------------------------------------------


def test_build_azel_map_bins_captures_to_their_cells(engine: Engine, tmp_path) -> None:
    map_id = _make_map(
        engine,
        frame="azel",
        metric="hi_intensity",
        center_x_deg=180.0,
        center_y_deg=45.0,
        extent_x_deg=40.0,
        extent_y_deg=40.0,
        step_deg=10.0,
    )
    for i, (az, el) in enumerate([(180.0, 45.0), (190.0, 45.0), (180.0, 55.0)]):
        _tag_capture(engine, map_id, az, el, _write_npz(tmp_path / f"c{i}.npz"))
    with Session(engine) as session:
        sky_map = session.get(SkyMap, map_id)
        station, location = default_station(session), default_location(session)
        gm = build_sky_map(session, sky_map, station, location)
    assert gm.n_samples == 3
    cx = int(np.argmin(np.abs(gm.x_axis - 180.0)))
    cy = int(np.argmin(np.abs(gm.y_axis - 45.0)))
    assert gm.observed[cy, cx]  # a pointing landed on the centre cell
    assert np.isnan(gm.grid).any()  # unobserved corners stay gaps


def test_build_galactic_map_projects_and_reduces(engine: Engine, tmp_path) -> None:
    map_id = _make_map(
        engine, frame="galactic", metric="hi_intensity", extent_x_deg=360.0, extent_y_deg=180.0
    )
    for i in range(3):
        _tag_capture(engine, map_id, 120.0 + 10 * i, 50.0, _write_npz(tmp_path / f"g{i}.npz"))
    with Session(engine) as session:
        sky_map = session.get(SkyMap, map_id)
        station, location = default_station(session), default_location(session)
        gm = build_sky_map(session, sky_map, station, location)
    assert gm.n_samples == 3  # all three reduced + projected to l/b


def test_build_peak_vlsr_metric_runs(engine: Engine, tmp_path) -> None:
    map_id = _make_map(
        engine, frame="azel", metric="peak_vlsr", center_x_deg=180.0, center_y_deg=45.0
    )
    _tag_capture(engine, map_id, 180.0, 45.0, _write_npz(tmp_path / "v.npz"))
    with Session(engine) as session:
        sky_map = session.get(SkyMap, map_id)
        station, location = default_station(session), default_location(session)
        gm = build_sky_map(session, sky_map, station, location)
    assert gm.n_samples == 1


def test_build_drops_missing_files(engine: Engine, tmp_path) -> None:
    map_id = _make_map(engine, frame="azel", center_x_deg=180.0, center_y_deg=45.0)
    _tag_capture(engine, map_id, 180.0, 45.0, _write_npz(tmp_path / "ok.npz"))
    # A tagged capture whose file is gone → dropped, not an error.
    with Session(engine) as session:
        gone = Capture(
            device="synthetic",
            path=str(tmp_path / "missing.npz"),
            format="npz_spectra",
            sky_map_id=map_id,
            map_az_deg=190.0,
            map_el_deg=45.0,
            start=datetime(2026, 7, 1, 3, tzinfo=UTC),
        )
        session.add(gone)
        session.commit()
        sky_map = session.get(SkyMap, map_id)
        station, location = default_station(session), default_location(session)
        gm = build_sky_map(session, sky_map, station, location)
    assert gm.n_samples == 1  # only the present file survived


# ---- pure raster decision ----------------------------------------------------------


def test_next_map_cell_skips_done_and_out_of_limits(engine: Engine) -> None:
    with Session(engine) as session:
        station = default_station(session)
        station.az_min_deg, station.az_max_deg = 170.0, 200.0
        station.el_min_deg, station.el_max_deg = 40.0, 60.0
    cells = [(160.0, 45.0), (180.0, 45.0), (190.0, 45.0)]  # first is out of az limits
    assert next_map_cell(cells, [], station) == (180.0, 45.0)
    assert next_map_cell(cells, [(180.0, 45.0)], station) == (190.0, 45.0)
    assert next_map_cell(cells, [(180.0, 45.0), (190.0, 45.0)], station) is None


def test_map_cells_counts_grid() -> None:
    sky_map = SkyMap(
        name="g",
        frame="azel",
        center_x_deg=180.0,
        center_y_deg=45.0,
        extent_x_deg=20.0,
        extent_y_deg=10.0,
        step_deg=10.0,
    )
    cells = map_cells(sky_map)
    assert len(cells) == 3 * 2  # (20/10+1) x (10/10+1)


# ---- raster runner (map_tick) with a sim rotator + a fake control channel ----------


def _sim_station(engine: Engine) -> None:
    with Session(engine) as session:
        station = session.exec(select(Station)).one()
        station.rotator_kind = "sim"
        session.add(station)
        session.commit()


def test_map_tick_rasters_a_cell_then_completes(
    engine: Engine, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _sim_station(engine)
    npz = _write_npz(tmp_path / "cell.npz")
    map_id = _make_map(
        engine,
        frame="azel",
        center_x_deg=180.0,
        center_y_deg=45.0,
        extent_x_deg=0.0,  # a single-cell grid
        extent_y_deg=0.0,
        step_deg=10.0,
        dwell_s=0.0,
    )

    def fake_ctl(_endpoint: str, request: dict[str, Any], timeout_ms: int = 0) -> dict[str, Any]:
        cmd = request.get("cmd")
        if cmd == "status":
            return {"ok": True, "capturing": False}
        if cmd == "start_capture":
            return {"ok": True}
        if cmd == "stop_capture":
            return {
                "ok": True,
                "format": "npz",
                "path": str(npz),
                "elapsed_s": 0.0,
                "bytes_written": npz.stat().st_size,
                "source": "synthetic",
            }
        return {"ok": False, "error": "unexpected"}

    monkeypatch.setattr(server_mapping, "ctl_request", fake_ctl)

    app = create_app(
        Settings(zmq_endpoint=DEAD_ENDPOINT, data_dir=str(tmp_path / "data")), engine=engine
    )
    app.state.mapping = MapRunState(active_map_id=map_id, cells_total=1)

    asyncio.run(map_tick(app))
    # One cell recorded, tagged into the map at the commanded az/el.
    with Session(engine) as session:
        tagged = session.exec(select(Capture).where(Capture.sky_map_id == map_id)).all()
        assert len(tagged) == 1
        assert tagged[0].map_az_deg == 180.0 and tagged[0].map_el_deg == 45.0
    assert app.state.mapping.cells_done == 1

    # Second tick: the only cell is done → the map is marked complete, raster clears.
    asyncio.run(map_tick(app))
    with Session(engine) as session:
        assert session.get(SkyMap, map_id).status == "done"
    assert app.state.mapping.active_map_id is None


def test_map_tick_ignores_when_another_capture_owns_the_sdr(
    engine: Engine, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _sim_station(engine)
    map_id = _make_map(engine, frame="azel", center_x_deg=180.0, center_y_deg=45.0, dwell_s=0.0)
    monkeypatch.setattr(
        server_mapping, "ctl_request", lambda *a, **k: {"ok": True, "capturing": True}
    )
    app = create_app(
        Settings(zmq_endpoint=DEAD_ENDPOINT, data_dir=str(tmp_path / "data")), engine=engine
    )
    app.state.mapping = MapRunState(active_map_id=map_id, cells_total=1)
    asyncio.run(map_tick(app))
    with Session(engine) as session:
        assert session.exec(select(Capture).where(Capture.sky_map_id == map_id)).all() == []


# ---- routes ------------------------------------------------------------------------


def test_create_and_view_map(client: TestClient, engine: Engine) -> None:
    resp = client.post(
        "/maps",
        data={"name": "galactic strip", "frame": "galactic", "metric": "hi_intensity"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    map_id = int(resp.headers["location"].split("/")[-1])
    assert client.get("/maps").status_code == 200
    assert client.get(f"/maps/{map_id}").status_code == 200
    api = client.get(f"/api/maps/{map_id}").json()
    assert api["name"] == "galactic strip" and api["frame"] == "galactic"
    assert api["n_cells_observed"] == 0  # nothing ingested yet
    assert client.get("/api/maps").json()[0]["id"] == map_id


def test_create_map_rejects_bad_frame_and_metric(client: TestClient) -> None:
    assert client.post("/maps", data={"name": "x", "frame": "equatorial"}).status_code == 422
    assert client.post("/maps", data={"name": "x", "metric": "bogus"}).status_code == 422


def test_image_png_renders_even_when_empty(client: TestClient) -> None:
    map_id = int(
        client.post("/maps", data={"name": "empty"}, follow_redirects=False)
        .headers["location"]
        .split("/")[-1]
    )
    resp = client.get(f"/api/maps/{map_id}/image.png")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    assert len(resp.content) > 0


def test_start_raster_requires_azel_and_rotator(client: TestClient, engine: Engine) -> None:
    # A galactic map can't be rastered.
    gal = int(
        client.post("/maps", data={"name": "g", "frame": "galactic"}, follow_redirects=False)
        .headers["location"]
        .split("/")[-1]
    )
    assert client.post(f"/maps/{gal}/start", follow_redirects=False).status_code == 409
    # An az/el map still needs a configured rotator.
    az = int(
        client.post("/maps", data={"name": "a", "frame": "azel"}, follow_redirects=False)
        .headers["location"]
        .split("/")[-1]
    )
    assert client.post(f"/maps/{az}/start", follow_redirects=False).status_code == 409
    _sim_station(engine)
    assert client.post(f"/maps/{az}/start", follow_redirects=False).status_code == 303
    assert client.app.state.mapping.active_map_id == az
    assert client.post(f"/maps/{az}/stop", follow_redirects=False).status_code == 303
    assert client.app.state.mapping.active_map_id is None


def test_ingest_campaign_tags_captures(client: TestClient, engine: Engine, tmp_path) -> None:
    with Session(engine) as session:
        source = session.exec(select(RadioSource)).first()
        campaign = Campaign(
            name="drift", source_id=source.id, fixed_az_deg=200.0, fixed_el_deg=30.0
        )
        session.add(campaign)
        session.commit()
        session.refresh(campaign)
        cap = Capture(
            device="synthetic",
            path=str(_write_npz(tmp_path / "drift.npz")),
            format="npz_spectra",
            campaign_id=campaign.id,
            start=datetime(2026, 7, 1, 3, tzinfo=UTC),
        )
        session.add(cap)
        session.commit()
        campaign_id = campaign.id

    map_id = int(
        client.post("/maps", data={"name": "gal", "frame": "galactic"}, follow_redirects=False)
        .headers["location"]
        .split("/")[-1]
    )
    resp = client.post(
        f"/maps/{map_id}/ingest", data={"campaign_id": campaign_id}, follow_redirects=False
    )
    assert resp.status_code == 303
    with Session(engine) as session:
        tagged = session.exec(select(Capture).where(Capture.sky_map_id == map_id)).all()
        assert len(tagged) == 1
        assert tagged[0].map_az_deg == 200.0 and tagged[0].map_el_deg == 30.0


def test_ingest_campaign_without_fixed_azel_is_422(client: TestClient, engine: Engine) -> None:
    with Session(engine) as session:
        source = session.exec(select(RadioSource)).first()
        campaign = Campaign(name="no-azel", source_id=source.id)  # no fixed az/el
        session.add(campaign)
        session.commit()
        campaign_id = campaign.id
    map_id = int(
        client.post("/maps", data={"name": "g", "frame": "galactic"}, follow_redirects=False)
        .headers["location"]
        .split("/")[-1]
    )
    assert (
        client.post(f"/maps/{map_id}/ingest", data={"campaign_id": campaign_id}).status_code == 422
    )
