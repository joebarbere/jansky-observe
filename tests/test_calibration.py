"""Calibration captures + epochs (roadmap M7)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlmodel import Session, select

from jansky_observe.config import Settings
from jansky_observe.db import init_db
from jansky_observe.models import CalibrationEpoch, Capture, Observation, RadioSource
from jansky_observe.server.app import create_app
from jansky_observe.server.routers.captures import latest_cal_epoch_id, register_stopped_capture

DEAD_ENDPOINT = "tcp://127.0.0.1:1"


@pytest.fixture()
def engine(tmp_path) -> Engine:
    return init_db(tmp_path)


@pytest.fixture()
def client(engine: Engine, tmp_path) -> TestClient:
    return TestClient(
        create_app(Settings(zmq_endpoint=DEAD_ENDPOINT, data_dir=str(tmp_path)), engine=engine)
    )


def _add_capture(engine: Engine, *, kind: str = "science", obs_id: int | None = None) -> int:
    with Session(engine) as session:
        capture = Capture(
            observation_id=obs_id,
            device="synthetic",
            path="/x/c.npz",
            format="npz_spectra",
            size_bytes=1,
            kind=kind,
        )
        session.add(capture)
        session.commit()
        session.refresh(capture)
        assert capture.id is not None
        return capture.id


def _observation(engine: Engine, status: str = "running") -> int:
    with Session(engine) as session:
        source = session.exec(select(RadioSource)).first()
        assert source is not None and source.id is not None
        obs = Observation(
            name="cal test",
            observation_type_id=1,
            station_id=1,
            location_id=1,
            source_id=source.id,
            status=status,
        )
        session.add(obs)
        session.commit()
        session.refresh(obs)
        assert obs.id is not None
        return obs.id


# --- epochs + provenance ---------------------------------------------------


def test_create_epoch_and_list(client: TestClient, engine: Engine) -> None:
    assert client.get("/api/calibration_epochs").json() == []
    resp = client.post(
        "/calibration/epochs", data={"notes": "50R load, gain 15"}, follow_redirects=False
    )
    assert resp.status_code == 303
    epochs = client.get("/api/calibration_epochs").json()
    assert len(epochs) == 1
    assert epochs[0]["notes"] == "50R load, gain 15"
    assert epochs[0]["complete"] is False  # no cal captures yet
    assert "Calibration" in client.get("/calibration").text


def test_latest_cal_epoch_id_helper(engine: Engine) -> None:
    with Session(engine) as s:
        assert latest_cal_epoch_id(s) is None
        s.add(CalibrationEpoch(notes="first"))
        s.commit()
        s.add(CalibrationEpoch(notes="second"))
        s.commit()
        latest = latest_cal_epoch_id(s)
    # The most recent epoch wins.
    with Session(engine) as s:
        newest = s.exec(select(CalibrationEpoch).order_by(CalibrationEpoch.id.desc())).first()  # type: ignore[attr-defined]
        assert latest == newest.id


def test_science_capture_stamped_with_latest_epoch(client: TestClient, engine: Engine) -> None:
    client.post("/calibration/epochs", data={"notes": "epoch A"})
    # A stop-registration of a science capture stamps the cal epoch in effect.
    status = {
        "ok": True,
        "format": "npz",
        "path": "/x/sci.npz",
        "bytes_written": 10,
        "elapsed_s": 4.0,
        "source": "airspy",
    }
    cap_id = register_stopped_capture(engine, status)
    assert cap_id is not None
    meta = client.get(f"/api/captures/{cap_id}").json()
    assert meta["kind"] == "science"
    assert meta["cal_epoch_id"] == client.get("/api/calibration_epochs").json()[0]["id"]


# --- marking capture kinds -------------------------------------------------


def test_mark_calibration_kind_attaches_to_epoch(client: TestClient, engine: Engine) -> None:
    obs = _observation(engine)
    cap = _add_capture(engine, obs_id=obs)
    client.post("/calibration/epochs", data={"notes": "e"})
    epoch_id = client.get("/api/calibration_epochs").json()[0]["id"]

    resp = client.post(f"/captures/{cap}/kind", data={"kind": "ref_load"}, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/observations/{obs}"
    meta = client.get(f"/api/captures/{cap}").json()
    assert meta["kind"] == "ref_load"
    assert meta["cal_epoch_id"] == epoch_id
    # The epoch now lists it under ref_load.
    assert client.get("/api/calibration_epochs").json()[0]["captures"]["ref_load"] == [cap]


def test_calibration_kind_without_epoch_is_409(client: TestClient, engine: Engine) -> None:
    cap = _add_capture(engine)
    resp = client.post(f"/captures/{cap}/kind", data={"kind": "cold_sky"}, follow_redirects=False)
    assert resp.status_code == 409


def test_unknown_kind_is_422(client: TestClient, engine: Engine) -> None:
    cap = _add_capture(engine)
    resp = client.post(f"/captures/{cap}/kind", data={"kind": "bogus"}, follow_redirects=False)
    assert resp.status_code == 422


def test_epoch_complete_when_all_three_kinds_present(client: TestClient, engine: Engine) -> None:
    client.post("/calibration/epochs", data={"notes": "full"})
    for kind in ("ref_load", "cold_sky", "hot_ground"):
        cap = _add_capture(engine)
        client.post(f"/captures/{cap}/kind", data={"kind": kind})
    assert client.get("/api/calibration_epochs").json()[0]["complete"] is True
