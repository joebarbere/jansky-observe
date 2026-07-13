"""Tests for M9 piece-3 drift tracking: the pure decision, the tick, the routes,
and the scheduler auto-slew — all against the in-process ``sim`` rotator."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlmodel import Session, select

from jansky_observe.config import Settings
from jansky_observe.db import init_db
from jansky_observe.models import Observation, ObservationType, RadioSource, Station
from jansky_observe.server import rotator as rotator_glue
from jansky_observe.server.app import create_app
from jansky_observe.server.tracking import (
    enable_tracking,
    needs_repoint,
    repoint_threshold_deg,
    tracking_tick,
)

DEAD_ENDPOINT = "tcp://127.0.0.1:1"
WHEN = datetime(2026, 1, 15, 3, 0, 0, tzinfo=UTC)


def _app(tmp_path, **station_kw: object) -> tuple[FastAPI, Engine]:
    engine = init_db(tmp_path)
    with Session(engine) as s:
        station = s.exec(select(Station)).one()
        station.rotator_kind = "sim"
        station.el_min_deg = -90.0  # so any source pointing is in-envelope by default
        for key, value in station_kw.items():
            setattr(station, key, value)
        s.add(station)
        s.commit()
    settings = Settings(zmq_endpoint=DEAD_ENDPOINT, data_dir=str(tmp_path))
    return create_app(settings, engine=engine), engine


def _running_obs(engine: Engine) -> tuple[int, int]:
    with Session(engine) as s:
        ot = s.exec(select(ObservationType)).first()
        src = s.exec(select(RadioSource).where(RadioSource.name == "Cygnus region HI")).one()
        assert ot is not None and ot.id is not None and src.id is not None
        obs = Observation(
            name="track",
            observation_type_id=ot.id,
            station_id=1,
            location_id=1,
            source_id=src.id,
            status="running",
            actual_start=WHEN,
        )
        s.add(obs)
        s.commit()
        s.refresh(obs)
        assert obs.id is not None
        return obs.id, src.id


# ---- pure decision ------------------------------------------------------------


def test_repoint_threshold_deg() -> None:
    assert repoint_threshold_deg(20.0, 0.2) == pytest.approx(4.0)
    assert repoint_threshold_deg(0.0) == 0.1  # floor


def test_needs_repoint() -> None:
    assert needs_repoint(100.0, 45.0, None, None, 5.0) is True  # never pointed
    assert needs_repoint(100.0, 45.0, 100.3, 45.0, 5.0) is False  # tiny drift
    assert needs_repoint(100.0, 45.0, 150.0, 45.0, 5.0) is True  # big drift


# ---- tracking_tick ------------------------------------------------------------


def test_tracking_tick_repoints_and_logs(tmp_path) -> None:
    app, engine = _app(tmp_path)
    obs_id, src_id = _running_obs(engine)
    enable_tracking(app, obs_id, src_id)

    asyncio.run(tracking_tick(app, now=WHEN))
    state = app.state.tracking
    assert state.last_az_deg is not None and state.note == "tracking"  # a re-point happened
    with Session(engine) as s:
        assert "Tracking re-point to Cygnus region HI" in s.get(Observation, obs_id).notes

    # A second tick at the same instant: no drift → no new re-point, still enabled.
    last_before = (state.last_az_deg, state.last_el_deg)
    asyncio.run(tracking_tick(app, now=WHEN))
    assert (state.last_az_deg, state.last_el_deg) == last_before
    assert state.enabled is True


def test_tracking_tick_autostops_when_observation_not_running(tmp_path) -> None:
    app, engine = _app(tmp_path)
    obs_id, src_id = _running_obs(engine)
    with Session(engine) as s:
        obs = s.get(Observation, obs_id)
        obs.status = "done"
        s.add(obs)
        s.commit()
    enable_tracking(app, obs_id, src_id)
    asyncio.run(tracking_tick(app, now=WHEN))
    assert app.state.tracking.enabled is False


def test_tracking_tick_holds_when_out_of_limits(tmp_path) -> None:
    # An impossible az window (the source cannot be in [359.9, 360]) → out of limits.
    app, engine = _app(tmp_path, az_min_deg=359.9, az_max_deg=360.0)
    obs_id, src_id = _running_obs(engine)
    enable_tracking(app, obs_id, src_id)
    asyncio.run(tracking_tick(app, now=WHEN))
    assert app.state.tracking.enabled is True  # held, not disabled
    assert app.state.tracking.note == "out-of-limits"
    with Session(engine) as s:
        assert "left the slew envelope" in s.get(Observation, obs_id).notes


# ---- routes -------------------------------------------------------------------


def test_tracking_start_requires_running_observation(tmp_path) -> None:
    app, engine = _app(tmp_path)
    with TestClient(app) as client:
        assert client.post("/rotator/tracking/start").status_code == 409  # no running obs
        _running_obs(engine)
        resp = client.post("/rotator/tracking/start", follow_redirects=False)
        assert resp.status_code == 303
        assert app.state.tracking.enabled is True
        assert client.get("/api/rotator").json()["tracking"]["enabled"] is True

        assert client.post("/rotator/tracking/stop", follow_redirects=False).status_code == 303
        assert app.state.tracking.enabled is False


def test_tracking_start_409_without_rotator(tmp_path) -> None:
    engine = init_db(tmp_path)  # station stays kind "none"
    settings = Settings(zmq_endpoint=DEAD_ENDPOINT, data_dir=str(tmp_path))
    app = create_app(settings, engine=engine)
    _running_obs(engine)
    with TestClient(app) as client:
        assert client.post("/rotator/tracking/start").status_code == 409


# ---- scheduler auto-slew ------------------------------------------------------


def test_scheduler_auto_slew_to_source(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    from jansky_observe.server.scheduler import _auto_slew_to_source

    app, engine = _app(tmp_path)
    _, src_id = _running_obs(engine)
    calls: list[tuple[float, float]] = []
    monkeypatch.setattr(rotator_glue, "slew", lambda station, az, el: calls.append((az, el)))
    _auto_slew_to_source(engine, src_id, WHEN)
    assert len(calls) == 1  # slewed to the source's computed az/el


def test_scheduler_auto_slew_noop_without_rotator(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jansky_observe.server.scheduler import _auto_slew_to_source

    engine = init_db(tmp_path)  # kind "none"
    _, src_id = _running_obs(engine)
    calls: list[object] = []
    monkeypatch.setattr(rotator_glue, "slew", lambda *a, **k: calls.append(a))
    _auto_slew_to_source(engine, src_id, WHEN)
    assert calls == []  # no rotator → no slew attempted
