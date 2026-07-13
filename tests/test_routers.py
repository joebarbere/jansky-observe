"""Tests for the M2 catalog/observation routers and the JSON API mirrors."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlmodel import Session, select

from jansky_observe.config import Settings
from jansky_observe.db import init_db
from jansky_observe.models import ObservationType, RadioSource, Station
from jansky_observe.seeds import RADIO_SOURCES
from jansky_observe.server.app import create_app

DEAD_ENDPOINT = "tcp://127.0.0.1:1"

CANNED_WEATHER: dict[str, Any] = {
    "now": {
        "time": "2026-07-12T01:00:00+00:00",
        "temp_c": 21.5,
        "wind_speed_ms": 2.0,
        "wind_dir_deg": 180.0,
        "sky_cover_pct": 25.0,
        "summary": "Clear",
        "humidity_pct": 55.0,
    },
    "hourly": [],
    "provider": "canned",
}


@pytest.fixture()
def engine(tmp_path) -> Engine:
    """A fully migrated + seeded database in a tmp dir."""
    return init_db(tmp_path)


@pytest.fixture()
def client(engine: Engine, tmp_path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """A TestClient over an app with an injected engine and canned weather."""
    monkeypatch.setattr(
        "jansky_observe.server.routers.get_weather",
        lambda lat, lon, hours=3: CANNED_WEATHER,
    )
    settings = Settings(zmq_endpoint=DEAD_ENDPOINT, data_dir=str(tmp_path))
    return TestClient(create_app(settings, engine=engine))


def _type_id(engine: Engine, name: str) -> int:
    with Session(engine) as session:
        obs_type = session.exec(select(ObservationType).where(ObservationType.name == name)).one()
        assert obs_type.id is not None
        return obs_type.id


def _source_id(engine: Engine, name: str) -> int:
    with Session(engine) as session:
        source = session.exec(select(RadioSource).where(RadioSource.name == name)).one()
        assert source.id is not None
        return source.id


def _create_observation(client: TestClient, engine: Engine, name: str = "test obs") -> int:
    """Create a planned observation through the wizard create endpoint."""
    resp = client.post(
        "/wizard/create",
        data={
            "name": name,
            "observation_type_id": _type_id(engine, "HI pointed — Cygnus region"),
            "location_id": 1,
            "source_id": _source_id(engine, "Cygnus region HI"),
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    return int(resp.headers["location"].split("/")[2])


# ---- catalog CRUD ---------------------------------------------------------------


def test_source_crud_round_trip(client: TestClient) -> None:
    resp = client.post(
        "/catalog/sources",
        data={"name": "Vir A", "kind": "point_source", "ra_deg": "187.7", "dec_deg": "12.39"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "Vir A" in client.get("/catalog/sources").text

    api = {s["name"]: s for s in client.get("/api/sources").json()}
    assert api["Vir A"]["ra_deg"] == pytest.approx(187.7)
    source_id = api["Vir A"]["id"]

    assert client.get(f"/catalog/sources/{source_id}").status_code == 200
    resp = client.post(
        f"/catalog/sources/{source_id}",
        data={"name": "Vir A", "kind": "point_source", "ra_deg": "187.71", "dec_deg": "12.39"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    api = {s["name"]: s for s in client.get("/api/sources").json()}
    assert api["Vir A"]["ra_deg"] == pytest.approx(187.71)


def test_observer_crud_round_trip(client: TestClient) -> None:
    resp = client.post(
        "/catalog/observers", data={"name": "Joe", "callsign": "KC3XYZ"}, follow_redirects=False
    )
    assert resp.status_code == 303
    body = client.get("/catalog/observers").text
    assert "Joe" in body
    assert "KC3XYZ" in body

    resp = client.post(
        "/catalog/observers/1", data={"name": "Joe B", "callsign": "KC3XYZ"}, follow_redirects=False
    )
    assert resp.status_code == 303
    assert "Joe B" in client.get("/catalog/observers").text


def test_location_create(client: TestClient) -> None:
    resp = client.post(
        "/catalog/locations",
        data={"name": "Field", "lat_deg": "40.1", "lon_deg": "-75.4", "elevation_m": "120"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "Field" in client.get("/catalog/locations").text


def test_types_page_shows_seeded_checklists_read_only(client: TestClient) -> None:
    body = client.get("/catalog/types").text
    assert "Sun pointing calibration" in body
    assert "Mast plumbed (torpedo level) and angle gauge zeroed" in body
    assert "HI pointed — Cygnus region" in body
    assert "required" in body
    assert "recommended" in body


def test_station_page_shows_offsets_and_rf_chain(client: TestClient) -> None:
    body = client.get("/station").text
    assert "Discovery Dish" in body
    assert "Δaz +0.00°" in body
    assert "Airspy Mini" in body  # RF chain ordered list
    assert "inline USB-C bias-tee injector" in body


def test_station_page_shows_uuid(client: TestClient, engine: Engine) -> None:
    with Session(engine) as s:
        uuid = s.exec(select(Station)).one().uuid
    body = client.get("/station").text
    assert uuid in body  # the permanent station ID is shown on the page


def test_api_station_identity(client: TestClient, engine: Engine) -> None:
    with Session(engine) as s:
        uuid = s.exec(select(Station)).one().uuid
    identity = client.get("/api/station").json()
    assert identity["uuid"] == uuid
    assert identity["name"] == "Discovery Dish"
    assert identity["software_version"]  # the running version
    assert identity["location"]["name"] == "Home"
    assert identity["location"]["lat_deg"] == pytest.approx(40.024)


def test_station_offsets_direct_update(client: TestClient) -> None:
    resp = client.post(
        "/station/offsets",
        data={"offset_az_deg": "2.5", "offset_el_deg": "-1.0"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    body = client.get("/station").text
    assert "Δaz +2.50°" in body
    assert "Δel -1.00°" in body


def test_station_page_offers_stellarium_url_field(client: TestClient) -> None:
    body = client.get("/station").text
    assert 'name="stellarium_url"' in body
    assert 'placeholder="http://desktop:8090"' in body


def test_station_stellarium_url_save_and_clear(client: TestClient, engine: Engine) -> None:
    resp = client.post(
        "/station/stellarium",
        data={"stellarium_url": "http://desktop:8090/"},  # trailing slash stripped
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert 'value="http://desktop:8090"' in client.get("/station").text
    with Session(engine) as session:
        assert session.exec(select(Station)).one().stellarium_url == "http://desktop:8090"

    client.post("/station/stellarium", data={"stellarium_url": "   "})  # empty clears
    with Session(engine) as session:
        assert session.exec(select(Station)).one().stellarium_url is None


# ---- whats_up + pointing ----------------------------------------------------------


def test_whats_up_returns_all_seeded_sources_sorted_by_elevation(client: TestClient) -> None:
    entries = client.get("/api/whats_up", params={"window_h": 8}).json()
    assert {e["source_name"] for e in entries} == {name for name, *_ in RADIO_SOURCES}
    els = [e["el_deg"] for e in entries]
    assert all(isinstance(el, float) for el in els)
    assert els == sorted(els, reverse=True)
    for entry in entries:
        assert "transit_utc" in entry
        assert "beam_crossing_min" in entry
        assert "raw_az_deg" in entry
        assert isinstance(entry["transit_within_window"], bool)


def test_whats_up_applies_station_offsets(client: TestClient, engine: Engine) -> None:
    client.post("/station/offsets", data={"offset_az_deg": "3.0", "offset_el_deg": "1.5"})
    entries = client.get("/api/whats_up").json()
    for entry in entries:
        assert entry["el_deg"] == pytest.approx(entry["raw_el_deg"] + 1.5)


def test_api_pointing_single_source(client: TestClient, engine: Engine) -> None:
    source_id = _source_id(engine, "Cas A")
    payload = client.get(f"/api/pointing/{source_id}").json()
    assert payload["source_name"] == "Cas A"
    assert isinstance(payload["az_deg"], float)
    assert isinstance(payload["el_deg"], float)
    # Cas A at dec 58.8° with the ~21° beam: crossing time well over 2 hours.
    assert payload["beam_crossing_min"] > 120.0
    assert client.get("/api/pointing/9999").status_code == 404


# ---- observations: pages + JSON mirrors --------------------------------------------


def test_observations_list_empty(client: TestClient) -> None:
    assert "No active observations" in client.get("/observations").text
    assert client.get("/api/observations").json() == []


def test_observation_pages_mirror_json(client: TestClient, engine: Engine) -> None:
    obs_id = _create_observation(client, engine, name="mirror check")
    api_list = client.get("/api/observations").json()
    assert [o["name"] for o in api_list] == ["mirror check"]
    assert api_list[0]["status"] == "planned"
    assert api_list[0]["type"] == "HI pointed — Cygnus region"
    assert api_list[0]["source"] == "Cygnus region HI"

    page = client.get("/observations").text
    assert "mirror check" in page
    assert "planned" in page

    detail = client.get(f"/api/observations/{obs_id}").json()
    assert detail["name"] == "mirror check"
    assert detail["source"]["name"] == "Cygnus region HI"
    assert len(detail["checklist"]) == 5  # the seeded HI checklist
    assert detail["required_ok"] is False

    detail_page = client.get(f"/observations/{obs_id}").text
    for item in detail["checklist"]:
        assert item["text"] in detail_page


def test_observations_list_newest_first(client: TestClient, engine: Engine) -> None:
    _create_observation(client, engine, name="first")
    _create_observation(client, engine, name="second")
    names = [o["name"] for o in client.get("/api/observations").json()]
    assert names == ["second", "first"]


def test_sky_chart_route(client: TestClient) -> None:
    body = client.get("/api/sky_chart").json()
    assert {"sources", "sun", "moon", "galactic_plane", "beam", "location", "station"} <= set(body)
    assert body["location"]["name"] == "Home"
    assert len(body["sources"]) >= 5  # the seeded catalog minus the Sun source
    assert all({"name", "kind", "az_deg", "el_deg"} <= set(s) for s in body["sources"])
    assert not any(s["kind"] == "sun" for s in body["sources"])  # Sun is its own symbol
    assert body["beam"] is None  # no running observation
    page = client.get("/sky").text
    assert "/static/skychart.js" in page and 'id="skychart"' in page


def test_archive_hides_from_list_and_mcp_then_restores(client: TestClient, engine: Engine) -> None:
    keep = _create_observation(client, engine, name="keep")
    junk = _create_observation(client, engine, name="qa junk")

    # Archive the junk observation.
    resp = client.post(f"/observations/{junk}/archive", follow_redirects=False)
    assert resp.status_code == 303

    # Hidden from the default HTML list and the MCP-facing JSON list.
    names = [o["name"] for o in client.get("/api/observations").json()]
    assert names == ["keep"]
    page = client.get("/observations").text
    assert "qa junk" not in page
    assert "Show archived (1)" in page

    # Revealed on request, with a restore control; the row still exists.
    shown = client.get("/observations?show_archived=1").text
    assert "qa junk" in shown
    assert f"/observations/{junk}/unarchive" in shown
    assert client.get(f"/api/observations/{junk}").json()["name"] == "qa junk"

    # Restore brings it back to the active list.
    resp = client.post(f"/observations/{junk}/unarchive", follow_redirects=False)
    assert resp.status_code == 303
    names = [o["name"] for o in client.get("/api/observations").json()]
    assert set(names) == {"keep", "qa junk"}
    assert keep  # (silence unused)


def test_api_checklist_tick_records_by_and_when(client: TestClient, engine: Engine) -> None:
    obs_id = _create_observation(client, engine)
    item = client.get(f"/api/observations/{obs_id}").json()["checklist"][0]
    state = client.post(
        f"/api/observations/{obs_id}/checklist/{item['state_id']}/tick", json={"by": "claude"}
    ).json()
    assert state["checked"] is True
    assert state["checked_by"] == "claude"
    assert state["checked_at"] is not None

    refreshed = client.get(f"/api/observations/{obs_id}").json()["checklist"][0]
    assert refreshed["checked"] is True
    assert refreshed["checked_by"] == "claude"


def test_api_append_note(client: TestClient, engine: Engine) -> None:
    obs_id = _create_observation(client, engine)
    resp = client.post(f"/api/observations/{obs_id}/notes", json={"text": "first note"})
    assert resp.json()["notes"] == "first note"
    resp = client.post(f"/api/observations/{obs_id}/notes", json={"text": "second note"})
    assert resp.json()["notes"] == "first note\n\nsecond note"


def test_html_notes_save_replaces(client: TestClient, engine: Engine) -> None:
    obs_id = _create_observation(client, engine)
    resp = client.post(f"/observations/{obs_id}/notes", data={"notes": "hand-edited"})
    assert resp.status_code == 200
    assert "hand-edited" in resp.text  # the re-rendered notes fragment
    assert client.get(f"/api/observations/{obs_id}").json()["notes"] == "hand-edited"


def test_observation_404s(client: TestClient) -> None:
    assert client.get("/observations/999").status_code == 404
    assert client.get("/api/observations/999").status_code == 404
    assert client.post("/observations/999/start").status_code == 404


def test_abort_from_planned(client: TestClient, engine: Engine) -> None:
    obs_id = _create_observation(client, engine)
    resp = client.post(f"/observations/{obs_id}/abort", follow_redirects=False)
    assert resp.status_code == 303
    assert client.get(f"/api/observations/{obs_id}").json()["status"] == "aborted"
    # aborted is terminal: no further transitions
    assert client.post(f"/observations/{obs_id}/abort").status_code == 409
    assert client.post(f"/observations/{obs_id}/start").status_code == 409
