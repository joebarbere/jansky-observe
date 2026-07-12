"""GPS-fix location routes (plan §4.5 — optional gpsd hardware).

HTML: ``POST /catalog/locations/from-gps`` backs the "Use GPS fix" button on
the locations page. It reads one fix from gpsd and creates a Location named
``GPS fix <UTC date>`` (``source="gps"``, never the default), or — with an
``update_id`` form field — updates that location's coordinates in place.
The response is a small htmx result partial; when gpsd is unavailable the
partial carries the friendly error inline (status 200, because htmx does not
swap error-status bodies by default).

JSON: ``POST /api/locations/from-gps`` mirrors it; body ``{"update_id": <id>}``
selects the update-in-place path. GpsUnavailable → 503 with the friendly
detail.

gpsd's socket comes from the ``JANSKY_OBSERVE_GPSD`` environment variable
(``"host:port"``, default ``"127.0.0.1:2947"``), read at request time — it is
deliberately not a :class:`~jansky_observe.config.Settings` field: GPS is
optional hardware for a future portable setup (plan §4.5), and reading the
environment per request means pointing at a different gpsd needs no server
restart (and tests can monkeypatch it).
"""

from __future__ import annotations

import os
from html import escape
from typing import Annotated, Any

from fastapi import APIRouter, Form, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlmodel import Session, select

from jansky_observe.gps import GpsFix, GpsUnavailable, read_fix
from jansky_observe.models import Location, utcnow
from jansky_observe.server.routers import SessionDep, get_or_404

__all__ = ["router"]

router = APIRouter(tags=["gps"])

GPSD_ENV = "JANSKY_OBSERVE_GPSD"
DEFAULT_GPSD = "127.0.0.1:2947"
_FIX_TIMEOUT_S = 5.0


class FromGpsBody(BaseModel):
    """Request body for ``POST /api/locations/from-gps`` (entirely optional)."""

    #: When set, update this Location in place instead of creating one.
    update_id: int | None = None


def _gpsd_socket() -> tuple[str, int]:
    """The gpsd host/port from ``JANSKY_OBSERVE_GPSD`` (read at request time)."""
    raw = os.environ.get(GPSD_ENV, DEFAULT_GPSD)
    host, _, port = raw.rpartition(":")
    return host or "127.0.0.1", int(port)


def _apply_fix(session: Session, fix: GpsFix, update_id: int | None) -> Location:
    """Create or update a Location from the fix and commit it.

    Without ``update_id`` the row is named ``GPS fix <UTC date>`` (date from
    the fix itself when gpsd reported one); a same-named row from earlier in
    the day is updated rather than violating the unique name. With
    ``update_id`` that row's coordinates are overwritten. Either way
    ``source`` becomes ``"gps"``; a 2D fix (no altitude) leaves the stored
    elevation untouched (0.0 for a brand-new row).
    """
    if update_id is not None:
        location = get_or_404(session, Location, update_id)
    else:
        name = f"GPS fix {fix.time_utc[:10] or utcnow().date().isoformat()}"
        existing = session.exec(select(Location).where(Location.name == name)).first()
        location = existing or Location(
            name=name,
            lat_deg=fix.lat_deg,
            lon_deg=fix.lon_deg,
            elevation_m=0.0,
            source="gps",
            is_default=False,
        )
    location.lat_deg = fix.lat_deg
    location.lon_deg = fix.lon_deg
    if fix.elevation_m is not None:
        location.elevation_m = fix.elevation_m
    location.source = "gps"
    session.add(location)
    session.commit()
    session.refresh(location)
    return location


# ---- HTML (the "Use GPS fix" button on the locations page) ------------------------


@router.post("/catalog/locations/from-gps", response_class=HTMLResponse)
def location_from_gps(session: SessionDep, update_id: Annotated[str, Form()] = "") -> HTMLResponse:
    """Read a gpsd fix and save it as a Location; returns an htmx result partial.

    When gpsd is unavailable the partial is the friendly error message itself
    (htmx swaps it into the result area; error statuses would be ignored).
    """
    host, port = _gpsd_socket()
    try:
        fix = read_fix(host=host, port=port, timeout_s=_FIX_TIMEOUT_S)
    except GpsUnavailable as exc:
        return HTMLResponse(f'<p class="error">{escape(str(exc))}</p>')
    location = _apply_fix(session, fix, int(update_id) if update_id.strip() else None)
    elev = "—" if fix.elevation_m is None else f"{location.elevation_m:.0f} m"
    return HTMLResponse(
        f'<p>Saved <a href="/catalog/locations/{location.id}">{escape(location.name)}</a>: '
        f"{location.lat_deg:.5f}°, {location.lon_deg:.5f}°, {elev} "
        f'({fix.mode}D fix) — <a href="/catalog/locations">refresh the list</a></p>'
    )


# ---- JSON API ---------------------------------------------------------------------


@router.post("/api/locations/from-gps")
def api_location_from_gps(session: SessionDep, body: FromGpsBody | None = None) -> dict[str, Any]:
    """Read a gpsd fix and create/update a Location (mirror of the button).

    503 with the friendly detail when gpsd is unreachable or fixless.
    """
    host, port = _gpsd_socket()
    try:
        fix = read_fix(host=host, port=port, timeout_s=_FIX_TIMEOUT_S)
    except GpsUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    location = _apply_fix(session, fix, None if body is None else body.update_id)
    return {**location.model_dump(), "fix_mode": fix.mode, "fix_time_utc": fix.time_utc}
