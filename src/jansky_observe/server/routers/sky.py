"""The in-UI alt/az sky chart (roadmap M7): a glanceable, offline "what's up".

``GET /api/sky_chart`` returns the az/el of the seeded catalog sources, the Sun
and Moon, the galactic plane, and — while a session runs — the dish's beam cone,
all with the station's Δaz/Δel offsets applied. ``GET /sky`` is the canvas page
that draws them (``static/skychart.js``). All positions come from astropy
(``astro/skychart.py``); nothing leaves the Pi.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from sqlmodel import col, select

from jansky_observe.astro.pointing import hpbw_deg
from jansky_observe.astro.skychart import sky_positions
from jansky_observe.models import Observation, RadioSource
from jansky_observe.server.live_badge import source_coord
from jansky_observe.server.routers import (
    TEMPLATES,
    SessionDep,
    default_location,
    default_station,
)

__all__ = ["router"]

router = APIRouter(tags=["sky"])


@router.get("/sky", response_class=HTMLResponse)
def sky_page(request: Request) -> HTMLResponse:
    """The always-available alt/az sky chart."""
    return TEMPLATES.TemplateResponse(request, "sky.html", {})


@router.get("/api/sky_chart")
def api_sky_chart(session: SessionDep) -> dict[str, Any]:
    """Alt/az positions for the sky chart: catalog sources (offsets applied),
    Sun, Moon, the galactic plane, and the beam cone at the running session's
    dialed pointing (if any). Fully offline (astropy)."""
    location = default_location(session)
    station = default_station(session)
    sources = session.exec(select(RadioSource)).all()
    # The Sun gets its own symbol, so drop the Sun *source* to avoid a double draw.
    coords = [(s.name, s.kind, source_coord(s)) for s in sources if s.kind != "sun"]

    beam: tuple[float, float] | None = None
    running = session.exec(
        select(Observation)
        .where(Observation.status == "running")
        .order_by(col(Observation.id).desc())
    ).first()
    if (
        running is not None
        and running.pointing_az_deg is not None
        and running.pointing_el_deg is not None
    ):
        beam = (running.pointing_az_deg, running.pointing_el_deg)

    payload = sky_positions(
        lat_deg=location.lat_deg,
        lon_deg=location.lon_deg,
        elevation_m=location.elevation_m,
        sources=coords,
        offset_az_deg=station.pointing_offset_az_deg,
        offset_el_deg=station.pointing_offset_el_deg,
        beam=beam,
        hpbw_deg=hpbw_deg(station.dish_diameter_m),
    )
    payload["location"] = {
        "name": location.name,
        "lat_deg": location.lat_deg,
        "lon_deg": location.lon_deg,
    }
    payload["station"] = {"name": station.name}
    return payload
