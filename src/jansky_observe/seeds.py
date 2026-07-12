"""Idempotent seed data: station, home location, source list, observing ladder.

Every function inserts-if-missing by natural key (the row's unique ``name``),
so :func:`seed_all` is safe to run on every server start. Seeded content
follows plan §3 (sources) and §5.4 (the observing-ladder ObservationTypes,
including the Sun pointing calibration whose Δaz/Δel offsets the server
stores on the Station record).
"""

from __future__ import annotations

from typing import Any

from sqlmodel import Session, select

from jansky_observe.models import (
    ChecklistTemplateItem,
    Location,
    ObservationType,
    RadioSource,
    Station,
)

__all__ = [
    "seed_all",
    "seed_locations",
    "seed_observation_types",
    "seed_sources",
    "seed_stations",
]

#: Default SDR settings for every H-line-chain type (Airspy Mini, plan §4.1).
HI_SDR_SETTINGS: dict[str, Any] = {
    "center_freq_hz": 1420.4e6,
    "sample_rate_hz": 3e6,
    "gain": 16,
}

#: Sun pointing calibration checklist, verbatim from plan §5.4 (text, required).
SUN_CAL_CHECKLIST: tuple[tuple[str, bool], ...] = (
    ("Mast plumbed (torpedo level) and angle gauge zeroed", True),
    ("Mast azimuth tape scale referenced to true north", True),
    ("Chain powered in Stage-1 order; injector current ~120 mA confirmed", True),
    ("Total-power readout running (live view or meter script)", True),
    ("Read Sun az/el prediction for now (re-read each round — it moves ~15°/hr)", True),
    ("Dial prediction onto scale + gauge; confirm power above cold-sky floor", True),
    ("Peak azimuth (±10° sweep, ~2° steps), then elevation; repeat both", True),
    ("Feed-shadow cross-check: shadow centered on dish at RF peak", False),
    ("Record Δaz/Δel offsets (scale/gauge vs predicted) into observation notes", True),
    ("Expected ~1–3 dB rise at peak — log the actual number", False),
)

#: Shared H-line session checklist (plan §5.1 step 4 + the bias-tee rule).
HI_CHECKLIST: tuple[tuple[str, bool], ...] = (
    ("H-line feed connected through the inline USB-C bias-tee injector", True),
    ("Injector ON; feed current ~120 mA confirmed", True),
    ("Airspy internal bias tee OFF — verified never enabled", True),
    ("tinySA sanity check done", False),
    ("Dish pointed at target; set az/el angles recorded", True),
)

TSYS_CHECKLIST: tuple[tuple[str, bool], ...] = (
    ("Point dish at ground; capture", True),
    ("Point dish at cold sky (high elevation, away from Sun and galactic plane); capture", True),
    ("Identical SDR settings for both captures confirmed", True),
)

#: Guided receiver-calibration checklist (roadmap M7). The three captures are
#: marked as their calibration kinds and attached to one calibration epoch.
CALIBRATION_CHECKLIST: tuple[tuple[str, bool], ...] = (
    ("Start a calibration epoch on the Calibration page", True),
    ("Reference load (50 Ω) on the SDR input; capture, then mark it ref_load", True),
    ("Point cold sky (high el, away from Sun/galactic plane); capture, mark cold_sky", True),
    ("Point warm ground; capture, mark hot_ground", True),
    ("Identical SDR settings across all three cal captures confirmed", True),
)

INJECTION_CHECKLIST: tuple[tuple[str, bool], ...] = (
    ("Attenuator pad stack in line — never connect HackRF TX output directly", True),
)

#: Guided before/after RFI survey around the 1420 MHz window (roadmap M6). The
#: two sweeps must share identical HackRF settings so the station can compare
#: them bin-for-bin.
RFI_SURVEY_1420_CHECKLIST: tuple[tuple[str, bool], ...] = (
    ("Antenna/feed connected through the normal Stage-1 chain (survey as observed)", True),
    ("Run the BEFORE sweep across the 1420 window (RFI-sweep button / start_rfi_sweep)", True),
    ("Note any bins standing above the noise floor near 1420.4 MHz", False),
    ("Run the observing session, or wait the comparison interval", False),
    ("Run the AFTER sweep with identical HackRF settings (start/stop/gain)", True),
    ("Review the before/after comparison on this page; flag new or grown interferers", True),
)

#: (name, description, default SDR settings, checklist) per plan §5.4.
OBSERVATION_TYPES: tuple[tuple[str, str, dict[str, Any], tuple[tuple[str, bool], ...]], ...] = (
    (
        "Sun pointing calibration",
        "Ladder win #1 and the prerequisite for everything pointed (build Stage 2.5). "
        "Peak the Sun in total power; the recorded Δaz/Δel offsets are stored on the "
        "Station record and applied to every future pointing display.",
        HI_SDR_SETTINGS,
        SUN_CAL_CHECKLIST,
    ),
    (
        "HI pointed — Cygnus region",
        "Ladder win #2: the first-light session. v1 classifier + HI4PI cross-check "
        "confirm it (plan §6).",
        HI_SDR_SETTINGS,
        HI_CHECKLIST,
    ),
    (
        "HI pointed — rotation curve longitudes",
        "Ladder win #3: repeat pointings along the galactic plane; analysis lives in "
        "jansky-research/ezRA.",
        HI_SDR_SETTINGS,
        HI_CHECKLIST,
    ),
    (
        "Continuum drift scan — Cas A / Cyg A",
        "Ladder win #4 (stretch): hours-long fixed-pointing total-power drift; the "
        "rise–plateau–fall is the detection.",
        HI_SDR_SETTINGS,
        HI_CHECKLIST,
    ),
    (
        "Tsys sky/ground pair",
        "Commissioning system-temperature measurement: a ground / cold-sky pointed-capture "
        "pair with identical SDR settings (plan §10.1; the sky_ground_tsys reduction lives "
        "in jansky-research).",
        HI_SDR_SETTINGS,
        TSYS_CHECKLIST,
    ),
    (
        "Calibration sweep",
        "Guided receiver calibration (roadmap M7): a ref-load (50 Ω), cold-sky, and "
        "hot-ground capture under one calibration epoch, so science captures carry cal-epoch "
        "provenance (plans 78/79). Start the epoch on the Calibration page, then mark each "
        "capture's kind from its list.",
        HI_SDR_SETTINGS,
        CALIBRATION_CHECKLIST,
    ),
    (
        "RFI survey",
        "HackRF hackrf_sweep pass over the band (e.g. 1–2 GHz) stored as a Capture — what "
        "does the window pass-through actually see.",
        {"sweep_start_hz": 1e9, "sweep_end_hz": 2e9},
        (),
    ),
    (
        "RFI survey @ 1420",
        "Guided before/after HackRF sweep around the 1420 MHz window (roadmap M6): run one "
        "sweep, observe (or wait), run another with identical settings; the station "
        "summarizes which bins are new or louder. What does the pass-through see, and did it "
        "change over the session?",
        {"sweep_start_hz": 1.30e9, "sweep_end_hz": 1.70e9},
        RFI_SURVEY_1420_CHECKLIST,
    ),
    (
        "injection test",
        "HackRF replays a synthetic 1420.4 MHz-offset tone/noise file into the chain via "
        "an attenuator pad stack to validate the whole stack end-to-end.",
        {},
        INJECTION_CHECKLIST,
    ),
)

#: (name, kind, ra_deg, dec_deg, gal_l_deg, gal_b_deg, notes) per plan §3.
RADIO_SOURCES: tuple[
    tuple[str, str, float | None, float | None, float | None, float | None, str], ...
] = (
    ("Sun", "sun", None, None, None, None, "Ephemeris computed live with astropy."),
    (
        "Cas A",
        "point_source",
        350.85,
        58.815,
        None,
        None,
        "Supernova remnant; 23h23m24s +58°48.9′.",
    ),
    (
        "Cyg A",
        "point_source",
        299.868,
        40.734,
        None,
        None,
        "Radio galaxy; 19h59m28.36s +40°44′02″.",
    ),
    ("Tau A", "point_source", 83.633, 22.014, None, None, "Crab Nebula; 05h34m32s +22°00′52″."),
    (
        "Cygnus region HI",
        "hi_region",
        None,
        None,
        80.0,
        0.0,
        "Galactic-plane HI; first-light target.",
    ),
    (
        "Galactic anticenter HI",
        "hi_region",
        None,
        None,
        180.0,
        0.0,
        "Galactic-plane HI, anticenter.",
    ),
)


def seed_stations(session: Session) -> None:
    """Insert the Discovery Dish station if missing.

    Parameters
    ----------
    session : Session
        An open SQLModel session; the caller commits.
    """
    if session.exec(select(Station).where(Station.name == "Discovery Dish")).first() is not None:
        return
    session.add(
        Station(
            name="Discovery Dish",
            dish_diameter_m=0.7,
            dish_f_d=0.38,
            feed="KrakenRF H-line feed (1420 MHz, LNA + filter integrated, 120 mA)",
            rf_chain=[
                "H-line feed (LNA + filter)",
                "inline USB-C bias-tee injector",
                "Airspy Mini",
                "Raspberry Pi 5",
            ],
            compute="Raspberry Pi 5",
            mount_type="manual alt-az",
            pointing_offset_az_deg=0.0,
            pointing_offset_el_deg=0.0,
        )
    )


def seed_locations(session: Session) -> None:
    """Insert the default "Home" location (Manayunk, Philadelphia) if missing.

    Parameters
    ----------
    session : Session
        An open SQLModel session; the caller commits.
    """
    if session.exec(select(Location).where(Location.name == "Home")).first() is not None:
        return
    session.add(
        Location(
            name="Home",
            lat_deg=40.024,
            lon_deg=-75.211,
            elevation_m=35.0,
            source="manual",
            address="Manayunk, Philadelphia, PA",
            is_default=True,
        )
    )


def seed_sources(session: Session) -> None:
    """Insert the seeded RadioSource list (plan §3) — insert-if-missing by name.

    Parameters
    ----------
    session : Session
        An open SQLModel session; the caller commits.
    """
    for name, kind, ra, dec, gal_l, gal_b, notes in RADIO_SOURCES:
        if session.exec(select(RadioSource).where(RadioSource.name == name)).first() is not None:
            continue
        session.add(
            RadioSource(
                name=name,
                kind=kind,
                ra_deg=ra,
                dec_deg=dec,
                gal_l_deg=gal_l,
                gal_b_deg=gal_b,
                notes=notes,
            )
        )


def seed_observation_types(session: Session) -> None:
    """Insert the observing-ladder + utility ObservationTypes (plan §5.4).

    Each type's ChecklistTemplateItem rows are created with it. Existing
    types (matched by name) are left untouched, checklists included.

    Parameters
    ----------
    session : Session
        An open SQLModel session; the caller commits.
    """
    for name, description, sdr_settings, checklist in OBSERVATION_TYPES:
        existing = session.exec(select(ObservationType).where(ObservationType.name == name)).first()
        if existing is not None:
            continue
        obs_type = ObservationType(
            name=name,
            description=description,
            default_sdr_settings=dict(sdr_settings),
        )
        session.add(obs_type)
        session.flush()  # assign obs_type.id for the checklist FKs
        assert obs_type.id is not None
        for order_index, (text, required) in enumerate(checklist):
            session.add(
                ChecklistTemplateItem(
                    observation_type_id=obs_type.id,
                    order_index=order_index,
                    text=text,
                    required=required,
                )
            )


def seed_all(session: Session) -> None:
    """Run every seeder and commit. Idempotent — safe on every server start.

    Parameters
    ----------
    session : Session
        An open SQLModel session; committed on success.
    """
    seed_stations(session)
    seed_locations(session)
    seed_sources(session)
    seed_observation_types(session)
    session.commit()
