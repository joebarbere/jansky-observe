"""Routers for the M2 observation-records UI + JSON API (plan §5.1, §5.3, §10 M2).

Shared plumbing lives here: the request-scoped DB session dependency (the
engine sits on ``app.state.engine``), the Jinja2 template environment used by
every page router, and the pointing/weather helpers the catalog, observation,
and wizard routers all render from.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, HTTPException, Request
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from jansky_observe.astro import (
    beam_crossing_minutes,
    hpbw_deg,
    pointing_now,
    rise_set_info,
    target_coord,
    transit_info,
)
from jansky_observe.models import Location, RadioSource, Station
from jansky_observe.weather import get_weather

__all__ = [
    "SessionDep",
    "TEMPLATES",
    "captures",
    "db_session",
    "default_location",
    "default_station",
    "get_or_404",
    "photos",
    "reports",
    "source_pointing",
    "weather_or_none",
]

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"


def _fmt_dt(value: datetime | None) -> str:
    """Render a (naive-UTC-by-convention) datetime as ``YYYY-MM-DD HH:MM UTC``."""
    if value is None:
        return "—"
    return value.strftime("%Y-%m-%d %H:%M UTC")


TEMPLATES = Jinja2Templates(directory=str(_TEMPLATES_DIR))
"""Shared template environment for every page router."""
TEMPLATES.env.filters["dt"] = _fmt_dt


def db_session(request: Request) -> Iterator[Session]:
    """Yield a request-scoped :class:`~sqlmodel.Session` on ``app.state.engine``."""
    with Session(request.app.state.engine) as session:
        yield session


SessionDep = Annotated[Session, Depends(db_session)]
"""Annotated dependency: an open session for the duration of one request."""


def get_or_404[T](session: Session, model: type[T], ident: int) -> T:
    """Fetch a row by primary key or raise a 404.

    Parameters
    ----------
    session : Session
        An open session.
    model : type
        The SQLModel table class.
    ident : int
        Primary key.

    Returns
    -------
    T
        The row.

    Raises
    ------
    HTTPException
        404 when no row has that primary key.
    """
    row = session.get(model, ident)
    if row is None:
        raise HTTPException(status_code=404, detail=f"{model.__name__} {ident} not found")
    return row


def default_station(session: Session) -> Station:
    """Return the station row (one row for now; 404 if the seed is missing)."""
    station = session.exec(select(Station)).first()
    if station is None:
        raise HTTPException(status_code=404, detail="no Station configured")
    return station


def default_location(session: Session) -> Location:
    """Return the default location ("Home"), or the first one (404 if none)."""
    location = session.exec(select(Location).where(Location.is_default)).first()
    if location is None:
        location = session.exec(select(Location)).first()
    if location is None:
        raise HTTPException(status_code=404, detail="no Location configured")
    return location


def _target_kind(source: RadioSource) -> str:
    """Map a RadioSource onto a :func:`target_coord` kind."""
    if source.kind == "sun":
        return "sun"
    if source.gal_l_deg is not None and source.gal_b_deg is not None:
        return "galactic"
    return "radec"


def source_pointing(
    source: RadioSource,
    location: Location,
    station: Station,
    when: datetime | None = None,
    full: bool = True,
) -> dict[str, Any]:
    """Everything the wizard shows for one source (plan §5.1 step 2).

    Parameters
    ----------
    source : RadioSource
        The target.
    location : Location
        Observing location (lat/lon/elevation).
    station : Station
        Supplies the Δaz/Δel pointing offsets (plan §5.4) and dish diameter.
    when : datetime, optional
        UTC time (default: now).
    full : bool
        When ``True`` also compute transit, rise/set, and beam-crossing
        (slower); ``False`` returns just the current-pointing block.

    Returns
    -------
    dict
        JSON-able pointing payload: displayed az/el (offsets applied), raw
        az/el, drift rates, and — when ``full`` — transit time/elevation,
        rise/set, and beam-crossing minutes.
    """
    coord = target_coord(
        _target_kind(source),
        ra_deg=source.ra_deg,
        dec_deg=source.dec_deg,
        gal_l_deg=source.gal_l_deg,
        gal_b_deg=source.gal_b_deg,
        when=when,
    )
    info = pointing_now(
        coord,
        location.lat_deg,
        location.lon_deg,
        location.elevation_m,
        when=when,
        offset_az_deg=station.pointing_offset_az_deg,
        offset_el_deg=station.pointing_offset_el_deg,
    )
    payload: dict[str, Any] = {
        "source_id": source.id,
        "source_name": source.name,
        "kind": source.kind,
        "az_deg": info.az_deg,
        "el_deg": info.el_deg,
        "raw_az_deg": info.raw_az_deg,
        "raw_el_deg": info.raw_el_deg,
        "drift_az_deg_per_min": info.drift_az_deg_per_min,
        "drift_el_deg_per_min": info.drift_el_deg_per_min,
        "is_up": info.is_up,
        "offset_az_deg": station.pointing_offset_az_deg,
        "offset_el_deg": station.pointing_offset_el_deg,
    }
    if not full:
        return payload

    import astropy.units as u
    from astropy.coordinates import EarthLocation

    earth = EarthLocation(
        lat=location.lat_deg * u.deg,
        lon=location.lon_deg * u.deg,
        height=location.elevation_m * u.m,
    )
    transit = transit_info(coord, earth, when=when)
    rise_set = rise_set_info(coord, earth, when=when)
    beam = hpbw_deg(station.dish_diameter_m)
    dec_deg = float(coord.icrs.dec.deg)
    payload.update(
        {
            "transit_utc": transit.time_utc.isoformat(),
            "transit_el_deg": transit.el_deg,
            "rise_utc": None if rise_set.rise_utc is None else rise_set.rise_utc.isoformat(),
            "set_utc": None if rise_set.set_utc is None else rise_set.set_utc.isoformat(),
            "always_up": rise_set.always_up,
            "never_up": rise_set.never_up,
            "hpbw_deg": beam,
            "beam_crossing_min": beam_crossing_minutes(dec_deg, hpbw_deg=beam),
        }
    )
    return payload


def weather_or_none(lat: float, lon: float, hours: int = 3) -> dict[str, Any] | None:
    """Best-effort weather snapshot — never raises, never blocks a page on failure.

    Parameters
    ----------
    lat, lon : float
        Station coordinates (degrees).
    hours : int
        Forecast hours (default 3, per the wizard, plan §5.1).

    Returns
    -------
    dict or None
        The :func:`~jansky_observe.weather.get_weather` snapshot, or ``None``
        when every provider fails (the UI shows "weather unavailable").
    """
    try:
        return get_weather(lat, lon, hours=hours)
    except Exception:  # noqa: BLE001 — weather is advisory; degrade, never block
        return None


# Exported last: the submodule imports the shared plumbing defined above.
from jansky_observe.server.routers import captures as captures  # noqa: E402
from jansky_observe.server.routers import gps as gps  # noqa: E402
from jansky_observe.server.routers import photos as photos  # noqa: E402
from jansky_observe.server.routers import reports as reports  # noqa: E402
