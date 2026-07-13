"""Unattended capture schedules (roadmap M7, plans 79/84).

CRUD for :class:`~jansky_observe.models.Schedule` rows and a read of what the
background scheduler is doing. The scheduler loop itself lives in
``server/scheduler.py``; here we manage the table and show the next windows. All
write verbs are HTML-only (plan §12.4).
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlmodel import col, select

from jansky_observe.astro.pointing import transit_info
from jansky_observe.models import RadioSource, Schedule, utcnow
from jansky_observe.server.live_badge import source_coord
from jansky_observe.server.routers import (
    TEMPLATES,
    SessionDep,
    default_location,
    get_or_404,
)
from jansky_observe.server.scheduler import firing_window

__all__ = ["router"]

router = APIRouter(tags=["schedules"])

_REPEATS = ("once", "daily")
_FORMATS = ("npz", "sigmf")


def _next_window(session: Any, schedule: Schedule, now: Any) -> dict[str, Any] | None:
    """The schedule's next firing window (transit-derived), or ``None``."""
    location = default_location(session)
    source = session.get(RadioSource, schedule.source_id)
    if source is None:
        return None
    import astropy.units as u
    from astropy.coordinates import EarthLocation

    earth = EarthLocation(
        lat=location.lat_deg * u.deg,
        lon=location.lon_deg * u.deg,
        height=location.elevation_m * u.m,
    )
    transit = transit_info(source_coord(source, now), earth, when=now).time_utc
    start, stop = firing_window(schedule.lead_min, schedule.run_min, transit)
    return {"transit": transit, "start": start, "stop": stop}


def _summary(session: Any, schedule: Schedule) -> dict[str, Any]:
    source = session.get(RadioSource, schedule.source_id)
    window = _next_window(session, schedule, utcnow()) if schedule.enabled else None
    return {
        "id": schedule.id,
        "name": schedule.name,
        "source": None if source is None else source.name,
        "lead_min": schedule.lead_min,
        "run_min": schedule.run_min,
        "format": schedule.format,
        "repeat": schedule.repeat,
        "enabled": schedule.enabled,
        "next_start": None if window is None else window["start"],
        "next_stop": None if window is None else window["stop"],
    }


@router.get("/schedules", response_class=HTMLResponse)
def schedules_page(request: Request, session: SessionDep) -> HTMLResponse:
    """Scheduled captures + the create form + what the scheduler is doing now."""
    schedules = session.exec(select(Schedule).order_by(col(Schedule.id))).all()
    sources = session.exec(select(RadioSource).order_by(col(RadioSource.name))).all()
    state = request.app.state.scheduler
    return TEMPLATES.TemplateResponse(
        request,
        "schedules.html",
        {
            "rows": [_summary(session, s) for s in schedules],
            "sources": sources,
            "repeats": _REPEATS,
            "formats": _FORMATS,
            "state": state,
        },
    )


@router.post("/schedules")
def create_schedule(
    session: SessionDep,
    name: Annotated[str, Form()],
    source_id: Annotated[int, Form()],
    lead_min: Annotated[float, Form()] = 5.0,
    run_min: Annotated[float, Form()] = 30.0,
    format: Annotated[str, Form()] = "npz",
    repeat: Annotated[str, Form()] = "daily",
) -> RedirectResponse:
    """Create a schedule (enabled). HTML-only."""
    if format not in _FORMATS:
        raise HTTPException(status_code=422, detail=f"format must be one of {_FORMATS}")
    if repeat not in _REPEATS:
        raise HTTPException(status_code=422, detail=f"repeat must be one of {_REPEATS}")
    if run_min <= 0:
        raise HTTPException(status_code=422, detail="run_min must be positive")
    get_or_404(session, RadioSource, source_id)
    schedule = Schedule(
        name=name.strip(),
        source_id=source_id,
        lead_min=lead_min,
        run_min=run_min,
        format=format,
        repeat=repeat,
    )
    session.add(schedule)
    session.commit()
    return RedirectResponse("/schedules", status_code=303)


@router.post("/schedules/{schedule_id}/toggle")
def toggle_schedule(session: SessionDep, schedule_id: int) -> RedirectResponse:
    """Enable/disable a schedule. HTML-only."""
    schedule = get_or_404(session, Schedule, schedule_id)
    schedule.enabled = not schedule.enabled
    session.add(schedule)
    session.commit()
    return RedirectResponse("/schedules", status_code=303)


@router.post("/schedules/{schedule_id}/delete")
def delete_schedule(session: SessionDep, schedule_id: int) -> RedirectResponse:
    """Delete a schedule (it never captured anything itself). HTML-only."""
    schedule = get_or_404(session, Schedule, schedule_id)
    session.delete(schedule)
    session.commit()
    return RedirectResponse("/schedules", status_code=303)


@router.get("/api/schedules")
def api_schedules(session: SessionDep) -> list[dict[str, Any]]:
    """All schedules with their next firing window."""
    schedules = session.exec(select(Schedule).order_by(col(Schedule.id))).all()
    return [_summary(session, s) for s in schedules]


@router.get("/api/scheduler_status")
def api_scheduler_status(request: Request) -> dict[str, Any]:
    """What the background scheduler is doing: the running schedule (if any), its
    stop time, and the last per-schedule notes."""
    state = request.app.state.scheduler
    return {
        "running_schedule_id": state.running_schedule_id,
        "stop_at": state.stop_at,
        "note": state.note,
        "notes": state.notes,
    }
