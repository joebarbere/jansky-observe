"""Tests for the M9 piece-2 rotator control routes (status, slew, stop, park,
config, slew-to-target) — all against the in-process ``sim`` rotator."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlmodel import Session, select

from jansky_observe.config import Settings
from jansky_observe.db import init_db
from jansky_observe.models import Observation, ObservationType, RadioSource, Station
from jansky_observe.server.app import create_app

DEAD_ENDPOINT = "tcp://127.0.0.1:1"


@pytest.fixture()
def engine(tmp_path) -> Engine:
    return init_db(tmp_path)


@pytest.fixture()
def client(engine: Engine, tmp_path) -> TestClient:
    settings = Settings(zmq_endpoint=DEAD_ENDPOINT, data_dir=str(tmp_path))
    return TestClient(create_app(settings, engine=engine))


def _configure(client: TestClient, **overrides: object) -> None:
    """POST a rotator config (defaults to the sim, generous el limits)."""
    form = {
        "rotator_kind": "sim",
        "az_min_deg": 0.0,
        "az_max_deg": 360.0,
        "el_min_deg": -90.0,
        "el_max_deg": 90.0,
        "park_az_deg": 10.0,
        "park_el_deg": 90.0,
    }
    form.update(overrides)
    resp = client.post("/station/rotator", data=form, follow_redirects=False)
    assert resp.status_code == 303


def _running_observation(engine: Engine) -> int:
    with Session(engine) as s:
        ot = s.exec(select(ObservationType)).first()
        src = s.exec(select(RadioSource).where(RadioSource.name == "Cygnus region HI")).one()
        assert ot is not None and ot.id is not None and src.id is not None
        obs = Observation(
            name="rot test",
            observation_type_id=ot.id,
            station_id=1,
            location_id=1,
            source_id=src.id,
            status="running",
            actual_start=datetime(2026, 1, 15, tzinfo=UTC),
        )
        s.add(obs)
        s.commit()
        s.refresh(obs)
        assert obs.id is not None
        return obs.id


# ---- config -------------------------------------------------------------------


def test_config_save_and_status(client: TestClient, engine: Engine) -> None:
    # Unconfigured: status reports manual, no position.
    status = client.get("/api/rotator").json()
    assert status["kind"] == "none" and status["configured"] is False
    assert status["position"] is None

    _configure(client)
    with Session(engine) as s:
        assert s.exec(select(Station)).one().rotator_kind == "sim"

    status = client.get("/api/rotator").json()
    assert status["configured"] is True and status["reachable"] is True
    assert status["position"]["el_deg"] == 90.0  # sim starts stowed straight up
    assert status["park"] == {"az_deg": 10.0, "el_deg": 90.0}


def test_config_rejects_bad_kind_and_limits(client: TestClient) -> None:
    assert client.post("/station/rotator", data={"rotator_kind": "bogus"}).status_code == 422
    assert (
        client.post(
            "/station/rotator",
            data={"rotator_kind": "sim", "az_min_deg": 300, "az_max_deg": 10},
        ).status_code
        == 422
    )


# ---- slew / stop / park -------------------------------------------------------


def test_slew_within_limits_moves_and_logs(client: TestClient, engine: Engine) -> None:
    _configure(client)
    obs_id = _running_observation(engine)
    resp = client.post(
        "/rotator/slew", data={"az_deg": 120.0, "el_deg": 30.0}, follow_redirects=False
    )
    assert resp.status_code == 303
    # the sim advances toward the target; readback is en route or arrived
    status = client.get("/api/rotator").json()
    assert 0.0 <= status["position"]["az_deg"] <= 120.0
    # the slew was logged to the running observation's timeline
    with Session(engine) as s:
        assert "Rotator slew to az 120.0" in s.get(Observation, obs_id).notes


def test_slew_outside_limits_is_refused(client: TestClient) -> None:
    _configure(client, el_min_deg=0.0)  # floor at the horizon
    resp = client.post("/rotator/slew", data={"az_deg": 120.0, "el_deg": -5.0})
    assert resp.status_code == 422
    assert "outside the station slew limits" in resp.json()["detail"]


def test_slew_without_rotator_is_409(client: TestClient) -> None:
    resp = client.post("/rotator/slew", data={"az_deg": 100.0, "el_deg": 45.0})
    assert resp.status_code == 409  # kind "none" → no rotator configured


def test_stop_and_park(client: TestClient) -> None:
    _configure(client)
    assert client.post("/rotator/stop", follow_redirects=False).status_code == 303
    assert client.post("/rotator/park", follow_redirects=False).status_code == 303


# ---- slew to target -----------------------------------------------------------


def test_slew_to_target_logs_source(client: TestClient, engine: Engine) -> None:
    _configure(client)  # el limits -90..90 so any computed pointing is in-envelope
    obs_id = _running_observation(engine)
    resp = client.post(f"/observations/{obs_id}/slew_to_target", follow_redirects=False)
    assert resp.status_code == 303
    with Session(engine) as s:
        notes = s.get(Observation, obs_id).notes
        assert "Cygnus region HI" in notes and "offsets applied" in notes


def test_slew_to_target_unknown_observation_404(client: TestClient) -> None:
    _configure(client)
    assert client.post("/observations/9999/slew_to_target").status_code == 404


# ---- station page -------------------------------------------------------------


def test_station_page_shows_rotator_config(client: TestClient) -> None:
    body = client.get("/station").text
    assert 'action="/station/rotator"' in body
    assert 'name="rotator_kind"' in body
