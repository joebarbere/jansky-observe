"""Model round-trip tests: every entity persists and reads back through a session."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import Engine
from sqlmodel import Session, SQLModel, create_engine, select

from jansky_observe.models import (
    Capture,
    ChecklistItemState,
    ChecklistTemplateItem,
    ClassifierResult,
    Location,
    Observation,
    ObservationObserver,
    ObservationType,
    Observer,
    Photo,
    RadioSource,
    Station,
)


def _engine(tmp_path: Path) -> Engine:
    engine = create_engine(f"sqlite:///{tmp_path / 'models.sqlite3'}")
    SQLModel.metadata.create_all(engine)
    return engine


def test_station_pointing_offsets_default_zero(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    with Session(engine) as s:
        s.add(Station(name="Discovery Dish", dish_diameter_m=0.7, dish_f_d=0.38))
        s.commit()
    with Session(engine) as s:
        station = s.exec(select(Station)).one()
        assert station.pointing_offset_az_deg == 0.0
        assert station.pointing_offset_el_deg == 0.0
        assert station.mount_type == "manual alt-az"


def test_station_rf_chain_json_round_trip(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    chain = ["H-line feed", "injector", "Airspy Mini", "Pi 5"]
    with Session(engine) as s:
        s.add(Station(name="s", dish_diameter_m=0.7, dish_f_d=0.38, rf_chain=chain))
        s.commit()
    with Session(engine) as s:
        assert s.exec(select(Station)).one().rf_chain == chain


def test_full_graph_round_trip(tmp_path: Path) -> None:
    """One row of every entity, wired together, survives a session round-trip."""
    engine = _engine(tmp_path)
    start = datetime(2026, 7, 12, 1, 30, tzinfo=UTC)
    with Session(engine) as s:
        station = Station(name="Discovery Dish", dish_diameter_m=0.7, dish_f_d=0.38)
        location = Location(
            name="Home", lat_deg=40.024, lon_deg=-75.211, elevation_m=35.0, is_default=True
        )
        observer = Observer(name="Joe", callsign="", email="joe.barbere@gmail.com")
        source = RadioSource(
            name="Cygnus region HI", kind="hi_region", gal_l_deg=80.0, gal_b_deg=0.0
        )
        obs_type = ObservationType(
            name="HI pointed", default_sdr_settings={"center_freq_hz": 1420.4e6}
        )
        s.add(station)
        s.add(location)
        s.add(observer)
        s.add(source)
        s.add(obs_type)
        s.flush()
        assert obs_type.id is not None
        item = ChecklistTemplateItem(
            observation_type_id=obs_type.id, order_index=0, text="feed connected", required=True
        )
        s.add(item)
        s.flush()
        assert None not in (station.id, location.id, source.id, observer.id, item.id)
        obs = Observation(
            name="first light",
            observation_type_id=obs_type.id,
            station_id=station.id,  # type: ignore[arg-type]
            location_id=location.id,  # type: ignore[arg-type]
            source_id=source.id,  # type: ignore[arg-type]
            status="running",
            actual_start=start,
            weather_snapshot={"temp_c": 21.5, "sky_cover": "clear"},
            pointing_az_deg=310.0,
            pointing_el_deg=55.0,
            computed_az_deg=311.2,
            computed_el_deg=54.6,
            notes="**first light**",
        )
        s.add(obs)
        s.flush()
        assert obs.id is not None
        s.add(ObservationObserver(observation_id=obs.id, observer_id=observer.id))  # type: ignore[arg-type]
        s.add(
            ChecklistItemState(
                observation_id=obs.id,
                template_item_id=item.id,  # type: ignore[arg-type]
                checked=True,
                checked_at=start,
                checked_by="Joe",
            )
        )
        capture = Capture(
            observation_id=obs.id,
            device="airspy",
            path="observations/1/spectra/a.npz",
            format="npz_spectra",
            size_bytes=4096,
            start=start,
            sdr_settings={"center_freq_hz": 1420.4e6, "bias_tee": False},
        )
        s.add(capture)
        s.flush()
        assert capture.id is not None
        s.add(
            ClassifierResult(
                capture_id=capture.id,
                name="hi_rule_v1",
                version="1.0",
                verdict="detected",
                score=7.2,
                params={"peak_freq_hz": 1420.35e6, "snr": 7.2},
                mode="live",
            )
        )
        s.add(
            Photo(observation_id=obs.id, path="observations/1/photos/dish.jpg", is_highlight=True)
        )
        s.commit()

    with Session(engine) as s:
        obs = s.exec(select(Observation)).one()
        assert obs.status == "running"
        assert obs.weather_snapshot == {"temp_c": 21.5, "sky_cover": "clear"}
        assert obs.pointing_az_deg == 310.0
        assert obs.computed_el_deg == 54.6
        assert obs.actual_start is not None
        assert obs.actual_start.replace(tzinfo=UTC) == start
        assert obs.planned_start is None
        link = s.exec(select(ObservationObserver)).one()
        assert link.observation_id == obs.id
        state = s.exec(select(ChecklistItemState)).one()
        assert state.checked and state.checked_by == "Joe"
        capture = s.exec(select(Capture)).one()
        assert capture.sdr_settings["bias_tee"] is False
        assert capture.format == "npz_spectra"
        result = s.exec(select(ClassifierResult)).one()
        assert result.verdict == "detected"
        assert result.params["snr"] == 7.2
        photo = s.exec(select(Photo)).one()
        assert photo.is_highlight
        source = s.exec(select(RadioSource)).one()
        assert source.ra_deg is None and source.gal_l_deg == 80.0


def test_capture_observation_fk_nullable(tmp_path: Path) -> None:
    """M1 capture files predate observations — no observation FK required."""
    engine = _engine(tmp_path)
    with Session(engine) as s:
        s.add(Capture(device="airspy", path="m1/legacy.sigmf-data", format="sigmf"))
        s.commit()
    with Session(engine) as s:
        assert s.exec(select(Capture)).one().observation_id is None
