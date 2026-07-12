"""Tests for the session start wizard (plan §5.1) and the Sun-cal flow (§5.4)."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlmodel import Session, select

from jansky_observe.astro.stellarium import StellariumUnavailable
from jansky_observe.config import Settings
from jansky_observe.db import init_db
from jansky_observe.models import Observation, ObservationType, RadioSource, Station
from jansky_observe.server.app import create_app

DEAD_ENDPOINT = "tcp://127.0.0.1:1"

CANNED_WEATHER: dict[str, Any] = {
    "now": {
        "time": "2026-07-12T01:00:00+00:00",
        "temp_c": 18.0,
        "wind_speed_ms": 3.2,
        "wind_dir_deg": 270.0,
        "sky_cover_pct": 40.0,
        "summary": "Partly cloudy",
        "humidity_pct": 60.0,
    },
    "hourly": [
        {
            "time": "2026-07-12T02:00:00+00:00",
            "temp_c": 17.0,
            "wind_speed_ms": 3.0,
            "wind_dir_deg": 270.0,
            "sky_cover_pct": 45.0,
            "summary": "Partly cloudy",
            "humidity_pct": 62.0,
            "precip_prob_pct": 10.0,
        }
    ],
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


def _id_of(engine: Engine, model: type, name: str) -> int:
    with Session(engine) as session:
        row = session.exec(select(model).where(model.name == name)).one()
        assert row.id is not None
        return int(row.id)


def _walk_to_step4(
    client: TestClient, engine: Engine, type_name: str, source_name: str
) -> tuple[int, list[dict[str, Any]]]:
    """Drive steps 1–3; return the observation id and its checklist."""
    type_id = _id_of(engine, ObservationType, type_name)
    source_id = _id_of(engine, RadioSource, source_name)
    resp = client.post(
        "/wizard/step2",
        data={"observation_type_id": type_id, "location_id": 1, "name": ""},
    )
    assert resp.status_code == 200
    resp = client.post(
        "/wizard/create",
        data={
            "name": "",
            "observation_type_id": type_id,
            "location_id": 1,
            "source_id": source_id,
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    obs_id = int(resp.headers["location"].split("/")[2])
    assert client.get(f"/wizard/{obs_id}/step3").status_code == 200
    resp = client.post(
        f"/wizard/{obs_id}/step3",
        data={"pointing_az_deg": "120.0", "pointing_el_deg": "45.0"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/wizard/{obs_id}/step4"
    checklist = client.get(f"/api/observations/{obs_id}").json()["checklist"]
    return obs_id, checklist


def _tick(client: TestClient, obs_id: int, state_id: int, by: str = "Joe") -> None:
    resp = client.post(
        f"/observations/{obs_id}/checklist/{state_id}?view=wizard",
        data={"checked": "on", "by": by},
    )
    assert resp.status_code == 200


# ---- steps 1–2 ------------------------------------------------------------------


def test_step1_offers_types_observers_locations(client: TestClient) -> None:
    body = client.get("/wizard").text
    assert "Sun pointing calibration" in body
    assert "HI pointed — Cygnus region" in body
    assert "Home" in body
    # default name is "<type> — <date>"
    assert 'name="name"' in body


def test_step2_lists_sources_and_carries_step1_fields(client: TestClient, engine: Engine) -> None:
    type_id = _id_of(engine, ObservationType, "HI pointed — Cygnus region")
    body = client.post(
        "/wizard/step2",
        data={"observation_type_id": type_id, "location_id": 1, "name": "my session"},
    ).text
    assert "Cygnus region HI" in body
    assert "Cas A" in body
    assert 'name="name" value="my session"' in body  # hidden carry-forward
    assert f'name="observation_type_id" value="{type_id}"' in body


def test_pointing_fragment_shows_pointing_and_weather(client: TestClient, engine: Engine) -> None:
    source_id = _id_of(engine, RadioSource, "Cyg A")
    body = client.get("/wizard/pointing", params={"source_id": source_id, "location_id": 1}).text
    assert "dial az / el" in body
    assert "raw az / el" in body
    assert "transit" in body
    assert "beam crossing" in body
    assert "Partly cloudy" in body  # canned weather rendered
    assert "canned" in body


def test_pointing_fragment_degrades_when_weather_fails(
    client: TestClient, engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(lat: float, lon: float, hours: int = 3) -> dict[str, Any]:
        raise RuntimeError("network down")

    monkeypatch.setattr("jansky_observe.server.routers.get_weather", boom)
    source_id = _id_of(engine, RadioSource, "Sun")
    resp = client.get("/wizard/pointing", params={"source_id": source_id, "location_id": 1})
    assert resp.status_code == 200
    assert "weather unavailable" in resp.text


# ---- the Stellarium block (plan §4.3): finder slew + az/el cross-check --------------


def _set_stellarium_url(engine: Engine, url: str | None = "http://desktop:8090") -> None:
    with Session(engine) as session:
        station = session.exec(select(Station)).one()
        station.stellarium_url = url
        session.add(station)
        session.commit()


def _install_fake_stellarium(
    monkeypatch: pytest.MonkeyPatch,
    *,
    find: bool = True,
    info: dict[str, Any] | None = None,
    fail: bool = False,
) -> dict[str, Any]:
    """Replace the wizard's StellariumClient with a canned, call-recording fake."""
    calls: dict[str, Any] = {"base_url": None, "focus": None, "slew": None}

    class FakeStellarium:
        def __init__(self, base_url: str, client: Any = None) -> None:
            calls["base_url"] = base_url

        def find_object(self, name: str) -> dict[str, Any] | None:
            if fail:
                raise StellariumUnavailable("Stellarium unreachable at http://desktop:8090")
            return {"name": name, "matches": [name]} if find else None

        def focus(self, target: str) -> bool:
            calls["focus"] = target
            return True

        def object_info(self, name: str) -> dict[str, Any]:
            return dict(info or {})

        def slew_view(self, az_deg: float, alt_deg: float) -> None:
            calls["slew"] = (az_deg, alt_deg)

    monkeypatch.setattr("jansky_observe.server.routers.wizard.StellariumClient", FakeStellarium)
    return calls


def test_pointing_fragment_without_stellarium_url_has_no_button(
    client: TestClient, engine: Engine
) -> None:
    source_id = _id_of(engine, RadioSource, "Cyg A")
    body = client.get("/wizard/pointing", params={"source_id": source_id, "location_id": 1}).text
    assert "Show in Stellarium" not in body


def test_pointing_fragment_with_stellarium_url_offers_button(
    client: TestClient, engine: Engine
) -> None:
    _set_stellarium_url(engine)
    source_id = _id_of(engine, RadioSource, "Cyg A")
    body = client.get("/wizard/pointing", params={"source_id": source_id, "location_id": 1}).text
    assert "Show in Stellarium" in body
    assert 'hx-post="/wizard/stellarium/show"' in body


def test_stellarium_show_without_url_is_409(client: TestClient, engine: Engine) -> None:
    source_id = _id_of(engine, RadioSource, "Cyg A")
    resp = client.post("/wizard/stellarium/show", data={"source_id": source_id, "location_id": 1})
    assert resp.status_code == 409


def test_stellarium_show_focuses_and_reports_agreement(
    client: TestClient, engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Focused by name; Stellarium's reported az/alt ≈ astropy's → 'agrees within 0.3°'."""
    _set_stellarium_url(engine)
    source_id = _id_of(engine, RadioSource, "Cyg A")
    pointing = client.get(f"/api/pointing/{source_id}").json()
    calls = _install_fake_stellarium(
        monkeypatch,
        info={"azimuth": pointing["raw_az_deg"], "altitude": pointing["raw_el_deg"]},
    )
    resp = client.post("/wizard/stellarium/show", data={"source_id": source_id, "location_id": 1})
    assert resp.status_code == 200
    assert "Stellarium focused on" in resp.text
    assert "agrees within 0.3°" in resp.text
    assert calls["base_url"] == "http://desktop:8090"
    assert calls["focus"] == "Cyg A"
    assert calls["slew"] is None  # focus path — no raw view slew


def test_stellarium_show_warns_on_big_delta(
    client: TestClient, engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A >1° disagreement renders a warning — astropy stays authoritative."""
    _set_stellarium_url(engine)
    source_id = _id_of(engine, RadioSource, "Cyg A")
    pointing = client.get(f"/api/pointing/{source_id}").json()
    _install_fake_stellarium(
        monkeypatch,
        info={"azimuth": pointing["raw_az_deg"], "altitude": pointing["raw_el_deg"] + 5.0},
    )
    body = client.post(
        "/wizard/stellarium/show", data={"source_id": source_id, "location_id": 1}
    ).text
    assert "Stellarium differs by 5.0°" in body
    assert "trust astropy" in body
    assert "warn-text" in body


def test_stellarium_show_falls_back_to_view_slew_when_find_fails(
    client: TestClient, engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pseudo-sources (the HI regions) aren't in Stellarium — slew to computed az/el."""
    _set_stellarium_url(engine)
    source_id = _id_of(engine, RadioSource, "Cygnus region HI")
    pointing = client.get(f"/api/pointing/{source_id}").json()
    calls = _install_fake_stellarium(monkeypatch, find=False)
    body = client.post(
        "/wizard/stellarium/show", data={"source_id": source_id, "location_id": 1}
    ).text
    assert "view slewed to az" in body
    assert calls["focus"] is None
    az_deg, el_deg = calls["slew"]
    # the fragment recomputes "now" a moment after /api/pointing — allow drift slack
    assert az_deg == pytest.approx(pointing["raw_az_deg"], abs=0.1)
    assert el_deg == pytest.approx(pointing["raw_el_deg"], abs=0.1)


def test_stellarium_show_degrades_when_info_lacks_altaz(
    client: TestClient, engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_stellarium_url(engine)
    source_id = _id_of(engine, RadioSource, "Cyg A")
    _install_fake_stellarium(monkeypatch, info={})
    body = client.post(
        "/wizard/stellarium/show", data={"source_id": source_id, "location_id": 1}
    ).text
    assert "no az/alt reported back" in body


def test_stellarium_show_unavailable_renders_inline_error(
    client: TestClient, engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dead Stellarium is a friendly inline message, never an error page."""
    _set_stellarium_url(engine)
    source_id = _id_of(engine, RadioSource, "Cyg A")
    _install_fake_stellarium(monkeypatch, fail=True)
    resp = client.post("/wizard/stellarium/show", data={"source_id": source_id, "location_id": 1})
    assert resp.status_code == 200
    assert "Stellarium unreachable" in resp.text
    assert "the wizard works fine without it" in resp.text


# ---- the end-to-end session ------------------------------------------------------


def test_wizard_end_to_end_hi_pointed(client: TestClient, engine: Engine) -> None:
    obs_id, checklist = _walk_to_step4(
        client, engine, "HI pointed — Cygnus region", "Cygnus region HI"
    )

    obs = client.get(f"/api/observations/{obs_id}").json()
    assert obs["status"] == "planned"
    assert obs["name"].startswith("HI pointed — Cygnus region — ")  # "<type> — <date>"
    assert obs["pointing_az_deg"] == pytest.approx(120.0)
    assert obs["pointing_el_deg"] == pytest.approx(45.0)
    assert len(checklist) == 5

    # Start button is disabled and Start is refused while a required item is unticked.
    required = [c for c in checklist if c["required"]]
    for item in required[:-1]:
        _tick(client, obs_id, item["state_id"])
    step4 = client.get(f"/wizard/{obs_id}/step4").text
    assert "disabled" in step4
    assert client.post(f"/observations/{obs_id}/start").status_code == 409

    _tick(client, obs_id, required[-1]["state_id"])
    step4 = client.get(f"/wizard/{obs_id}/step4").text
    assert 'id="btn-start-obs"' in step4
    assert "disabled" not in step4

    resp = client.post(f"/observations/{obs_id}/start", follow_redirects=False)
    assert resp.status_code == 303

    obs = client.get(f"/api/observations/{obs_id}").json()
    assert obs["status"] == "running"
    assert obs["actual_start"] is not None
    assert obs["weather_snapshot"] == CANNED_WEATHER
    assert isinstance(obs["computed_az_deg"], float)
    assert isinstance(obs["computed_el_deg"], float)

    # Running observations cannot be started again; the wizard bounces to detail.
    assert client.post(f"/observations/{obs_id}/start").status_code == 409
    bounce = client.get(f"/wizard/{obs_id}/step4", follow_redirects=False)
    assert bounce.status_code == 303
    assert bounce.headers["location"] == f"/observations/{obs_id}"

    resp = client.post(f"/observations/{obs_id}/stop", follow_redirects=False)
    assert resp.status_code == 303
    obs = client.get(f"/api/observations/{obs_id}").json()
    assert obs["status"] == "done"
    assert obs["actual_end"] is not None
    # teardown notes prompt on the detail page after stop
    assert "teardown" in client.get(f"/observations/{obs_id}?stopped=1").text

    # done is terminal for stop
    assert client.post(f"/observations/{obs_id}/stop").status_code == 409


def test_checklist_tick_persists_who_and_when_and_unticks(
    client: TestClient, engine: Engine
) -> None:
    obs_id, checklist = _walk_to_step4(
        client, engine, "HI pointed — Cygnus region", "Cygnus region HI"
    )
    state_id = checklist[0]["state_id"]
    _tick(client, obs_id, state_id, by="Joe")
    item = client.get(f"/api/observations/{obs_id}").json()["checklist"][0]
    assert item["checked"] is True
    assert item["checked_by"] == "Joe"
    assert item["checked_at"] is not None

    # untick: the checkbox submits nothing when cleared
    resp = client.post(f"/observations/{obs_id}/checklist/{state_id}?view=wizard", data={})
    assert resp.status_code == 200
    item = client.get(f"/api/observations/{obs_id}").json()["checklist"][0]
    assert item["checked"] is False
    assert item["checked_by"] == ""
    assert item["checked_at"] is None


def test_checklist_state_must_belong_to_observation(client: TestClient, engine: Engine) -> None:
    obs_a, checklist_a = _walk_to_step4(
        client, engine, "HI pointed — Cygnus region", "Cygnus region HI"
    )
    obs_b, _ = _walk_to_step4(client, engine, "Tsys sky/ground pair", "Cas A")
    state_a = checklist_a[0]["state_id"]
    resp = client.post(f"/observations/{obs_b}/checklist/{state_a}", data={"checked": "on"})
    assert resp.status_code == 404


# ---- Sun-cal completion writes the station offsets (plan §5.4) ---------------------


def test_suncal_completion_writes_offsets_and_appends_note(
    client: TestClient, engine: Engine
) -> None:
    obs_id, checklist = _walk_to_step4(client, engine, "Sun pointing calibration", "Sun")
    for item in checklist:
        if item["required"]:
            _tick(client, obs_id, item["state_id"])
    assert client.post(f"/observations/{obs_id}/start", follow_redirects=False).status_code == 303

    # offsets form is not offered before completion
    assert (
        client.post(
            f"/observations/{obs_id}/suncal",
            data={"offset_az_deg": "1.5", "offset_el_deg": "-0.75"},
        ).status_code
        == 409
    )

    assert client.post(f"/observations/{obs_id}/stop", follow_redirects=False).status_code == 303
    detail = client.get(f"/observations/{obs_id}").text
    assert "Sun-cal pointing offsets" in detail  # the Δaz/Δel form, with old values shown
    assert "az +0.00°" in detail

    resp = client.post(
        f"/observations/{obs_id}/suncal",
        data={"offset_az_deg": "1.5", "offset_el_deg": "-0.75"},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    with Session(engine) as session:
        station = session.exec(select(Station)).one()
        assert station.pointing_offset_az_deg == pytest.approx(1.5)
        assert station.pointing_offset_el_deg == pytest.approx(-0.75)

    notes = client.get(f"/api/observations/{obs_id}").json()["notes"]
    assert "pointing offsets updated az=+1.50° el=-0.75° (was az=+0.00° el=+0.00°)" in notes

    # the new offsets shape every future pointing display
    pointing = client.get("/api/pointing/2").json()
    assert pointing["el_deg"] == pytest.approx(pointing["raw_el_deg"] - 0.75, abs=1e-6)
    assert pointing["offset_az_deg"] == pytest.approx(1.5)


def test_suncal_route_rejects_other_types(client: TestClient, engine: Engine) -> None:
    obs_id, checklist = _walk_to_step4(
        client, engine, "HI pointed — Cygnus region", "Cygnus region HI"
    )
    for item in checklist:
        if item["required"]:
            _tick(client, obs_id, item["state_id"])
    client.post(f"/observations/{obs_id}/start")
    client.post(f"/observations/{obs_id}/stop")
    resp = client.post(
        f"/observations/{obs_id}/suncal",
        data={"offset_az_deg": "1.0", "offset_el_deg": "1.0"},
    )
    assert resp.status_code == 409


def test_wizard_state_lives_in_the_observation_row(client: TestClient, engine: Engine) -> None:
    """No server-side session machinery: the planned row *is* the wizard state."""
    obs_id, _ = _walk_to_step4(client, engine, "HI pointed — Cygnus region", "Cygnus region HI")
    with Session(engine) as session:
        observation = session.get(Observation, obs_id)
        assert observation is not None
        assert observation.status == "planned"
        assert observation.pointing_az_deg == pytest.approx(120.0)
    # revisiting step 3 pre-fills the previously dialed values
    assert 'value="120.0"' in client.get(f"/wizard/{obs_id}/step3").text
