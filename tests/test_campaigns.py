"""Drift-scan campaigns (roadmap M7)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlmodel import Session, select

from jansky_observe.config import Settings
from jansky_observe.db import init_db
from jansky_observe.models import Capture, RadioSource
from jansky_observe.server.app import create_app
from jansky_observe.server.routers.captures import active_campaign, register_stopped_capture

DEAD_ENDPOINT = "tcp://127.0.0.1:1"


@pytest.fixture()
def engine(tmp_path) -> Engine:
    return init_db(tmp_path)


@pytest.fixture()
def client(engine: Engine, tmp_path) -> TestClient:
    return TestClient(
        create_app(Settings(zmq_endpoint=DEAD_ENDPOINT, data_dir=str(tmp_path)), engine=engine)
    )


def _source_id(engine: Engine) -> int:
    with Session(engine) as session:
        source = session.exec(select(RadioSource)).first()
        assert source is not None and source.id is not None
        return source.id


def _tagged_capture(engine: Engine, campaign_id: int, sidereal_day: int) -> int:
    with Session(engine) as session:
        capture = Capture(
            device="synthetic",
            path=f"/x/{sidereal_day}.npz",
            format="npz_spectra",
            size_bytes=1,
            campaign_id=campaign_id,
            sidereal_day=sidereal_day,
            start=datetime(2026, 7, 1, 3, 0, tzinfo=UTC),
        )
        session.add(capture)
        session.commit()
        session.refresh(capture)
        assert capture.id is not None
        return capture.id


# --- create / status -------------------------------------------------------


def test_create_campaign_and_list(client: TestClient, engine: Engine) -> None:
    assert client.get("/api/campaigns").json() == []
    resp = client.post(
        "/campaigns",
        data={
            "name": "Cyg drift",
            "source_id": _source_id(engine),
            "fixed_az_deg": "90",
            "fixed_el_deg": "45",
            "notes": "nightly",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    cid = int(resp.headers["location"].split("/")[-1])
    rows = client.get("/api/campaigns").json()
    assert len(rows) == 1 and rows[0]["name"] == "Cyg drift" and rows[0]["status"] == "active"
    assert rows[0]["fixed_az_deg"] == 90.0
    assert "Cyg drift" in client.get(f"/campaigns/{cid}").text


def test_create_campaign_unknown_source_404(client: TestClient) -> None:
    resp = client.post(
        "/campaigns", data={"name": "x", "source_id": "9999"}, follow_redirects=False
    )
    assert resp.status_code == 404


def test_status_toggle_and_active_helper(client: TestClient, engine: Engine) -> None:
    cid = int(
        client.post(
            "/campaigns",
            data={"name": "c", "source_id": _source_id(engine)},
            follow_redirects=False,
        )
        .headers["location"]
        .split("/")[-1]
    )
    with Session(engine) as s:
        assert active_campaign(s) is not None
    client.post(f"/campaigns/{cid}/status", data={"status": "done"})
    with Session(engine) as s:
        assert active_campaign(s) is None  # none active now
    assert client.post(f"/campaigns/{cid}/status", data={"status": "bogus"}).status_code == 422


# --- tagging + passes ------------------------------------------------------


def test_stopped_capture_tagged_into_active_campaign(client: TestClient, engine: Engine) -> None:
    client.post("/campaigns", data={"name": "c", "source_id": _source_id(engine)})
    cap_id = register_stopped_capture(
        engine,
        {
            "ok": True,
            "format": "npz",
            "path": "/x/s.npz",
            "bytes_written": 3,
            "elapsed_s": 2.0,
            "source": "airspy",
        },
    )
    assert cap_id is not None
    meta = client.get(f"/api/captures/{cap_id}").json()
    assert meta["campaign_id"] == client.get("/api/campaigns").json()[0]["id"]
    assert meta["sidereal_day"] is not None


def test_no_active_campaign_leaves_capture_untagged(client: TestClient, engine: Engine) -> None:
    cap_id = register_stopped_capture(
        engine,
        {
            "ok": True,
            "format": "npz",
            "path": "/x/s.npz",
            "bytes_written": 3,
            "elapsed_s": 2.0,
            "source": "airspy",
        },
    )
    meta = client.get(f"/api/captures/{cap_id}").json()
    assert meta["campaign_id"] is None and meta["sidereal_day"] is None


def test_passes_grouped_by_sidereal_day(client: TestClient, engine: Engine) -> None:
    cid = int(
        client.post(
            "/campaigns",
            data={"name": "c", "source_id": _source_id(engine)},
            follow_redirects=False,
        )
        .headers["location"]
        .split("/")[-1]
    )
    _tagged_capture(engine, cid, sidereal_day=100)
    _tagged_capture(engine, cid, sidereal_day=100)
    _tagged_capture(engine, cid, sidereal_day=101)
    detail = client.get(f"/api/campaigns/{cid}").json()
    assert detail["n_captures"] == 3
    assert detail["n_passes"] == 2  # two sidereal days
    passes = {p["sidereal_day"]: len(p["captures"]) for p in detail["passes"]}
    assert passes == {101: 1, 100: 2}
    # Each capture carries its LST for stacking.
    assert all("lst_hours" in c for p in detail["passes"] for c in p["captures"])


def test_manual_attach_and_detach(client: TestClient, engine: Engine) -> None:
    cid = int(
        client.post(
            "/campaigns",
            data={"name": "c", "source_id": _source_id(engine)},
            follow_redirects=False,
        )
        .headers["location"]
        .split("/")[-1]
    )
    cap = _tagged_capture(engine, cid, sidereal_day=100)
    # Detach (campaign_id 0).
    client.post(f"/captures/{cap}/campaign", data={"campaign_id": "0"})
    assert client.get(f"/api/captures/{cap}").json()["campaign_id"] is None
    # Re-attach — the sidereal day is recomputed from the capture start.
    client.post(f"/captures/{cap}/campaign", data={"campaign_id": str(cid)})
    meta = client.get(f"/api/captures/{cap}").json()
    assert meta["campaign_id"] == cid and meta["sidereal_day"] is not None
