"""Seed-data tests: the observing ladder (§5.4), source list, station, idempotency."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import Engine
from sqlmodel import select

from jansky_observe import db
from jansky_observe.models import (
    ChecklistTemplateItem,
    Location,
    ObservationType,
    RadioSource,
    Station,
)
from jansky_observe.seeds import OBSERVATION_TYPES, seed_all


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    return db.init_db(tmp_path / "data")


def _checklist(engine: Engine, type_name: str) -> list[ChecklistTemplateItem]:
    with db.session(engine) as s:
        obs_type = s.exec(select(ObservationType).where(ObservationType.name == type_name)).one()
        return list(
            s.exec(
                select(ChecklistTemplateItem)
                .where(ChecklistTemplateItem.observation_type_id == obs_type.id)
                .order_by(ChecklistTemplateItem.order_index)  # type: ignore[arg-type]
            ).all()
        )


def test_station_seeded_with_zero_pointing_offsets(engine: Engine) -> None:
    with db.session(engine) as s:
        station = s.exec(select(Station)).one()
    assert station.name == "Discovery Dish"
    assert station.dish_diameter_m == 0.7
    assert station.dish_f_d == 0.38
    assert station.pointing_offset_az_deg == 0.0
    assert station.pointing_offset_el_deg == 0.0
    assert station.rf_chain[-1] == "Raspberry Pi 5"


def test_home_location_seeded_as_default(engine: Engine) -> None:
    with db.session(engine) as s:
        home = s.exec(select(Location).where(Location.name == "Home")).one()
    assert home.is_default
    assert home.source == "manual"
    assert home.lat_deg == pytest.approx(40.024)
    assert home.lon_deg == pytest.approx(-75.211)
    assert home.elevation_m == pytest.approx(35.0)


def test_radio_sources_seeded(engine: Engine) -> None:
    with db.session(engine) as s:
        sources = {src.name: src for src in s.exec(select(RadioSource)).all()}
    assert set(sources) == {
        "Sun",
        "Cas A",
        "Cyg A",
        "Tau A",
        "Cygnus region HI",
        "Galactic anticenter HI",
    }
    assert sources["Sun"].kind == "sun"
    assert sources["Sun"].ra_deg is None  # ephemeris computed live
    assert sources["Cas A"].ra_deg == pytest.approx(350.85)
    assert sources["Cas A"].dec_deg == pytest.approx(58.815)
    assert sources["Cyg A"].ra_deg == pytest.approx(299.868)
    assert sources["Tau A"].dec_deg == pytest.approx(22.014)
    assert sources["Cygnus region HI"].kind == "hi_region"
    assert sources["Cygnus region HI"].gal_l_deg == pytest.approx(80.0)
    assert sources["Galactic anticenter HI"].gal_l_deg == pytest.approx(180.0)


def test_observation_types_seeded(engine: Engine) -> None:
    expected = {name for name, _, _, _ in OBSERVATION_TYPES}
    assert expected == {
        "Sun pointing calibration",
        "HI pointed — Cygnus region",
        "HI pointed — rotation curve longitudes",
        "Continuum drift scan — Cas A / Cyg A",
        "Tsys sky/ground pair",
        "RFI survey",
        "injection test",
    }
    with db.session(engine) as s:
        seeded = {t.name for t in s.exec(select(ObservationType)).all()}
    assert seeded == expected


def test_hi_types_default_sdr_settings(engine: Engine) -> None:
    with db.session(engine) as s:
        for name in ("HI pointed — Cygnus region", "HI pointed — rotation curve longitudes"):
            obs_type = s.exec(select(ObservationType).where(ObservationType.name == name)).one()
            assert obs_type.default_sdr_settings == {
                "center_freq_hz": 1420.4e6,
                "sample_rate_hz": 3e6,
                "gain": 16,
            }


def test_sun_cal_checklist_ten_items_in_order(engine: Engine) -> None:
    items = _checklist(engine, "Sun pointing calibration")
    assert len(items) == 10
    assert [i.order_index for i in items] == list(range(10))
    # Items 8 (feed-shadow cross-check) and 10 (log the dB rise) are
    # recommended; everything else is required (plan §5.4).
    assert [i.required for i in items] == [
        True,
        True,
        True,
        True,
        True,
        True,
        True,
        False,
        True,
        False,
    ]
    assert items[0].text.startswith("Mast plumbed")
    assert "Δaz/Δel offsets" in items[8].text
    assert "true north" in items[1].text


def test_hi_checklist_covers_bias_tee_rule(engine: Engine) -> None:
    items = _checklist(engine, "HI pointed — Cygnus region")
    texts = [i.text for i in items]
    assert any("~120 mA" in t for t in texts)
    bias = next(i for i in items if "internal bias tee OFF" in i.text)
    assert bias.required


def test_tsys_checklist(engine: Engine) -> None:
    items = _checklist(engine, "Tsys sky/ground pair")
    assert [i.required for i in items] == [True, True, True]
    assert "ground" in items[0].text
    assert "cold sky" in items[1].text


def test_seed_all_rerun_is_noop(engine: Engine) -> None:
    def _counts() -> tuple[int, ...]:
        with db.session(engine) as s:
            return (
                len(s.exec(select(Station)).all()),
                len(s.exec(select(Location)).all()),
                len(s.exec(select(RadioSource)).all()),
                len(s.exec(select(ObservationType)).all()),
                len(s.exec(select(ChecklistTemplateItem)).all()),
            )

    before = _counts()
    with db.session(engine) as s:
        seed_all(s)
    assert _counts() == before
