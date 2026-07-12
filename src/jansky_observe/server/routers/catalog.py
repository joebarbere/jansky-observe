"""Catalog pages: radio sources, observers, observation types, locations, station.

Server-rendered CRUD (plan §2 UI stack): list pages carry an inline create
form; edit pages are plain forms. Every successful POST redirects (303) back
to the list so refresh is safe. The station page shows the dish, the RF chain
as an ordered list, and the current Δaz/Δel pointing offsets prominently
(plan §5.4); the offsets are editable here and also written by the Sun-cal
completion flow.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlmodel import col, select

from jansky_observe.models import (
    ChecklistTemplateItem,
    Location,
    ObservationType,
    Observer,
    RadioSource,
    utcnow,
)
from jansky_observe.server.routers import TEMPLATES, SessionDep, default_station, get_or_404

__all__ = ["router"]

router = APIRouter(tags=["catalog"])


def _opt_float(value: str) -> float | None:
    """Parse an optional form float — empty/whitespace becomes ``None``."""
    value = value.strip()
    return float(value) if value else None


def _see_other(url: str) -> RedirectResponse:
    """A 303 redirect (POST → GET) back to a page."""
    return RedirectResponse(url, status_code=303)


# ---- radio sources -----------------------------------------------------------


@router.get("/catalog/sources", response_class=HTMLResponse)
def sources_list(request: Request, session: SessionDep) -> HTMLResponse:
    """List all radio sources with an inline create form."""
    sources = session.exec(select(RadioSource).order_by(RadioSource.name)).all()
    return TEMPLATES.TemplateResponse(request, "sources_list.html", {"sources": sources})


@router.post("/catalog/sources")
def source_create(
    session: SessionDep,
    name: Annotated[str, Form()],
    kind: Annotated[str, Form()] = "custom",
    ra_deg: Annotated[str, Form()] = "",
    dec_deg: Annotated[str, Form()] = "",
    gal_l_deg: Annotated[str, Form()] = "",
    gal_b_deg: Annotated[str, Form()] = "",
    notes: Annotated[str, Form()] = "",
) -> RedirectResponse:
    """Create a radio source and redirect to the list."""
    session.add(
        RadioSource(
            name=name.strip(),
            kind=kind,
            ra_deg=_opt_float(ra_deg),
            dec_deg=_opt_float(dec_deg),
            gal_l_deg=_opt_float(gal_l_deg),
            gal_b_deg=_opt_float(gal_b_deg),
            notes=notes,
        )
    )
    session.commit()
    return _see_other("/catalog/sources")


@router.get("/catalog/sources/{source_id}", response_class=HTMLResponse)
def source_edit(request: Request, session: SessionDep, source_id: int) -> HTMLResponse:
    """Edit form for one radio source."""
    source = get_or_404(session, RadioSource, source_id)
    return TEMPLATES.TemplateResponse(request, "source_form.html", {"source": source})


@router.post("/catalog/sources/{source_id}")
def source_update(
    session: SessionDep,
    source_id: int,
    name: Annotated[str, Form()],
    kind: Annotated[str, Form()],
    ra_deg: Annotated[str, Form()] = "",
    dec_deg: Annotated[str, Form()] = "",
    gal_l_deg: Annotated[str, Form()] = "",
    gal_b_deg: Annotated[str, Form()] = "",
    notes: Annotated[str, Form()] = "",
) -> RedirectResponse:
    """Update a radio source and redirect to the list."""
    source = get_or_404(session, RadioSource, source_id)
    source.name = name.strip()
    source.kind = kind
    source.ra_deg = _opt_float(ra_deg)
    source.dec_deg = _opt_float(dec_deg)
    source.gal_l_deg = _opt_float(gal_l_deg)
    source.gal_b_deg = _opt_float(gal_b_deg)
    source.notes = notes
    session.add(source)
    session.commit()
    return _see_other("/catalog/sources")


# ---- observers -----------------------------------------------------------------


@router.get("/catalog/observers", response_class=HTMLResponse)
def observers_list(request: Request, session: SessionDep) -> HTMLResponse:
    """List all observers with an inline create form."""
    observers = session.exec(select(Observer).order_by(Observer.name)).all()
    return TEMPLATES.TemplateResponse(request, "observers_list.html", {"observers": observers})


@router.post("/catalog/observers")
def observer_create(
    session: SessionDep,
    name: Annotated[str, Form()],
    callsign: Annotated[str, Form()] = "",
    email: Annotated[str, Form()] = "",
    notes: Annotated[str, Form()] = "",
) -> RedirectResponse:
    """Create an observer and redirect to the list."""
    session.add(Observer(name=name.strip(), callsign=callsign, email=email, notes=notes))
    session.commit()
    return _see_other("/catalog/observers")


@router.get("/catalog/observers/{observer_id}", response_class=HTMLResponse)
def observer_edit(request: Request, session: SessionDep, observer_id: int) -> HTMLResponse:
    """Edit form for one observer."""
    observer = get_or_404(session, Observer, observer_id)
    return TEMPLATES.TemplateResponse(request, "observer_form.html", {"observer": observer})


@router.post("/catalog/observers/{observer_id}")
def observer_update(
    session: SessionDep,
    observer_id: int,
    name: Annotated[str, Form()],
    callsign: Annotated[str, Form()] = "",
    email: Annotated[str, Form()] = "",
    notes: Annotated[str, Form()] = "",
) -> RedirectResponse:
    """Update an observer and redirect to the list."""
    observer = get_or_404(session, Observer, observer_id)
    observer.name = name.strip()
    observer.callsign = callsign
    observer.email = email
    observer.notes = notes
    session.add(observer)
    session.commit()
    return _see_other("/catalog/observers")


# ---- observation types (checklist templates read-only, from seeds) -----------


@router.get("/catalog/types", response_class=HTMLResponse)
def types_list(request: Request, session: SessionDep) -> HTMLResponse:
    """List observation types with their checklist templates (read-only)."""
    types = session.exec(select(ObservationType).order_by(ObservationType.name)).all()
    checklists = {
        t.id: session.exec(
            select(ChecklistTemplateItem)
            .where(ChecklistTemplateItem.observation_type_id == t.id)
            .order_by(col(ChecklistTemplateItem.order_index))
        ).all()
        for t in types
    }
    return TEMPLATES.TemplateResponse(
        request, "types_list.html", {"types": types, "checklists": checklists}
    )


# ---- locations ----------------------------------------------------------------


@router.get("/catalog/locations", response_class=HTMLResponse)
def locations_list(request: Request, session: SessionDep) -> HTMLResponse:
    """List all locations with an inline create form."""
    locations = session.exec(select(Location).order_by(Location.name)).all()
    return TEMPLATES.TemplateResponse(request, "locations_list.html", {"locations": locations})


@router.post("/catalog/locations")
def location_create(
    session: SessionDep,
    name: Annotated[str, Form()],
    lat_deg: Annotated[float, Form()],
    lon_deg: Annotated[float, Form()],
    elevation_m: Annotated[float, Form()] = 0.0,
    address: Annotated[str, Form()] = "",
) -> RedirectResponse:
    """Create a location and redirect to the list."""
    session.add(
        Location(
            name=name.strip(),
            lat_deg=lat_deg,
            lon_deg=lon_deg,
            elevation_m=elevation_m,
            source="manual",
            address=address,
        )
    )
    session.commit()
    return _see_other("/catalog/locations")


@router.get("/catalog/locations/{location_id}", response_class=HTMLResponse)
def location_edit(request: Request, session: SessionDep, location_id: int) -> HTMLResponse:
    """Edit form for one location."""
    location = get_or_404(session, Location, location_id)
    return TEMPLATES.TemplateResponse(request, "location_form.html", {"location": location})


@router.post("/catalog/locations/{location_id}")
def location_update(
    session: SessionDep,
    location_id: int,
    name: Annotated[str, Form()],
    lat_deg: Annotated[float, Form()],
    lon_deg: Annotated[float, Form()],
    elevation_m: Annotated[float, Form()] = 0.0,
    address: Annotated[str, Form()] = "",
) -> RedirectResponse:
    """Update a location and redirect to the list."""
    location = get_or_404(session, Location, location_id)
    location.name = name.strip()
    location.lat_deg = lat_deg
    location.lon_deg = lon_deg
    location.elevation_m = elevation_m
    location.address = address
    session.add(location)
    session.commit()
    return _see_other("/catalog/locations")


# ---- station ------------------------------------------------------------------


@router.get("/station", response_class=HTMLResponse)
def station_page(request: Request, session: SessionDep) -> HTMLResponse:
    """The station page: dish info, RF chain as an ordered list, pointing offsets."""
    station = default_station(session)
    return TEMPLATES.TemplateResponse(request, "station.html", {"station": station})


@router.post("/station/offsets")
def station_offsets(
    session: SessionDep,
    offset_az_deg: Annotated[float, Form()],
    offset_el_deg: Annotated[float, Form()],
) -> RedirectResponse:
    """Set the station Δaz/Δel pointing offsets directly (plan §5.4)."""
    station = default_station(session)
    station.pointing_offset_az_deg = offset_az_deg
    station.pointing_offset_el_deg = offset_el_deg
    station.updated_at = utcnow()
    session.add(station)
    session.commit()
    return _see_other("/station")
