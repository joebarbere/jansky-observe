"""Observation pages, status transitions, and the JSON API (plan §5.3, §2).

HTML: list (status-badged, newest first), detail (metadata, pointing block,
weather snapshot, live checklist ticking via htmx, notes editor, captures),
and the start/stop/abort transitions. The Sun pointing calibration completion
offers Δaz/Δel inputs that write the station pointing offsets (plan §5.4).

JSON: the ``/api`` mirrors of the same data — the exact surfaces the M3 MCP
tools will proxy (plan §12.4).
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy import func
from sqlmodel import Session, col, select

from jansky_observe.capture.hackrf_sweep import rfi_sweep_comparison
from jansky_observe.models import (
    CAPTURE_KINDS,
    Capture,
    ChecklistItemState,
    ChecklistTemplateItem,
    ClassifierResult,
    Location,
    Observation,
    ObservationObserver,
    ObservationType,
    Observer,
    RadioSource,
    utcnow,
)
from jansky_observe.server.routers import (
    TEMPLATES,
    SessionDep,
    default_location,
    default_station,
    get_or_404,
    source_pointing,
    weather_or_none,
)

__all__ = ["SUN_CAL_TYPE_NAME", "router"]

router = APIRouter(tags=["observations"])

SUN_CAL_TYPE_NAME = "Sun pointing calibration"
"""The ObservationType whose completion writes the station Δaz/Δel (plan §5.4)."""


# ---- shared queries ------------------------------------------------------------


def checklist_rows(
    session: Session, observation: Observation
) -> list[tuple[ChecklistItemState, ChecklistTemplateItem]]:
    """The observation's checklist: (state, template item) pairs in template order."""
    rows = session.exec(
        select(ChecklistItemState, ChecklistTemplateItem)
        .where(ChecklistItemState.observation_id == observation.id)
        .where(ChecklistItemState.template_item_id == ChecklistTemplateItem.id)
        .order_by(col(ChecklistTemplateItem.order_index))
    ).all()
    return list(rows)


def required_items_ticked(session: Session, observation: Observation) -> bool:
    """``True`` when every required checklist item of the observation is ticked."""
    return all(
        state.checked for state, item in checklist_rows(session, observation) if item.required
    )


def observation_observers(session: Session, observation: Observation) -> list[Observer]:
    """The observers linked to an observation (M2M), ordered by name."""
    rows = session.exec(
        select(Observer)
        .where(ObservationObserver.observation_id == observation.id)
        .where(ObservationObserver.observer_id == Observer.id)
        .order_by(col(Observer.name))
    ).all()
    return list(rows)


def _detail_context(session: Session, observation: Observation) -> dict[str, Any]:
    """Template context shared by the detail page and its htmx fragments."""
    captures = session.exec(
        select(Capture).where(Capture.observation_id == observation.id).order_by(col(Capture.id))
    ).all()
    capture_ids = [c.id for c in captures if c.id is not None]
    capture_results: dict[int, list[ClassifierResult]] = {}
    if capture_ids:
        results = session.exec(
            select(ClassifierResult)
            .where(col(ClassifierResult.capture_id).in_(capture_ids))
            .order_by(col(ClassifierResult.id))
        ).all()
        for result in results:
            capture_results.setdefault(result.capture_id, []).append(result)
    return {
        "obs": observation,
        "obs_type": get_or_404(session, ObservationType, observation.observation_type_id),
        "source": get_or_404(session, RadioSource, observation.source_id),
        "location": get_or_404(session, Location, observation.location_id),
        "observers": observation_observers(session, observation),
        "checklist": checklist_rows(session, observation),
        "required_ok": required_items_ticked(session, observation),
        "captures": captures,
        "capture_results": capture_results,
        "capture_kinds": CAPTURE_KINDS,
        "rfi_comparison": rfi_sweep_comparison(captures),
    }


def _append_note(observation: Observation, text: str) -> None:
    """Append a paragraph to the observation's markdown notes."""
    observation.notes = f"{observation.notes.rstrip()}\n\n{text}".strip()
    observation.updated_at = utcnow()


def _see_other(url: str) -> RedirectResponse:
    """A 303 redirect (POST → GET)."""
    return RedirectResponse(url, status_code=303)


# ---- transitions (shared by the wizard and the detail page) ---------------------


def start_observation(session: Session, observation: Observation) -> None:
    """Start a planned observation (plan §5.1 step 4).

    Sets status ``running`` and ``actual_start`` (now UTC), saves the weather
    snapshot (best-effort — never blocks on network), and stores the computed
    (raw astrometric) az/el of the source at start.

    Raises
    ------
    HTTPException
        409 when the observation is not ``planned`` or a required checklist
        item is unticked.
    """
    if observation.status != "planned":
        raise HTTPException(
            status_code=409, detail=f"cannot start a {observation.status!r} observation"
        )
    if not required_items_ticked(session, observation):
        raise HTTPException(status_code=409, detail="required checklist items are unticked")
    source = get_or_404(session, RadioSource, observation.source_id)
    location = get_or_404(session, Location, observation.location_id)
    station = default_station(session)
    pointing = source_pointing(source, location, station, full=False)
    observation.status = "running"
    observation.actual_start = utcnow()
    observation.weather_snapshot = weather_or_none(location.lat_deg, location.lon_deg)
    observation.computed_az_deg = pointing["raw_az_deg"]
    observation.computed_el_deg = pointing["raw_el_deg"]
    observation.updated_at = utcnow()
    session.add(observation)
    session.commit()


# ---- HTML pages ------------------------------------------------------------------


@router.get("/observations", response_class=HTMLResponse)
def observations_list(
    request: Request, session: SessionDep, show_archived: int = 0
) -> HTMLResponse:
    """Active observations, newest first, status-badged. Archived (soft-deleted)
    observations are hidden by default; ``?show_archived=1`` reveals them in a
    separate, restorable section (roadmap M6)."""
    ordered = select(Observation).order_by(
        col(Observation.created_at).desc(), col(Observation.id).desc()
    )
    active = session.exec(ordered.where(col(Observation.archived_at).is_(None))).all()
    archived = (
        session.exec(ordered.where(col(Observation.archived_at).is_not(None))).all()
        if show_archived
        else []
    )
    archived_count = session.exec(
        select(func.count())
        .select_from(Observation)
        .where(col(Observation.archived_at).is_not(None))
    ).one()
    types = {t.id: t.name for t in session.exec(select(ObservationType)).all()}
    sources = {s.id: s.name for s in session.exec(select(RadioSource)).all()}
    return TEMPLATES.TemplateResponse(
        request,
        "observations_list.html",
        {
            "observations": active,
            "archived": archived,
            "archived_count": archived_count,
            "show_archived": bool(show_archived),
            "types": types,
            "sources": sources,
        },
    )


@router.post("/observations/{obs_id}/archive")
def observation_archive(session: SessionDep, obs_id: int) -> RedirectResponse:
    """Soft-delete: hide the observation from the default lists and the MCP
    surface. Restorable — the row and all its provenance are untouched
    (roadmap M6). HTML-only, never an MCP verb (same principle as photo delete)."""
    observation = get_or_404(session, Observation, obs_id)
    if observation.archived_at is None:
        observation.archived_at = utcnow()
        observation.updated_at = utcnow()
        session.add(observation)
        session.commit()
    return _see_other("/observations")


@router.post("/observations/{obs_id}/unarchive")
def observation_unarchive(session: SessionDep, obs_id: int) -> RedirectResponse:
    """Restore a soft-deleted observation (roadmap M6). HTML-only."""
    observation = get_or_404(session, Observation, obs_id)
    if observation.archived_at is not None:
        observation.archived_at = None
        observation.updated_at = utcnow()
        session.add(observation)
        session.commit()
    return _see_other(f"/observations/{obs_id}")


@router.get("/observations/{obs_id}", response_class=HTMLResponse)
def observation_detail(
    request: Request, session: SessionDep, obs_id: int, stopped: int = 0
) -> HTMLResponse:
    """Detail page: metadata, pointing, weather, checklist, notes, captures."""
    observation = get_or_404(session, Observation, obs_id)
    context = _detail_context(session, observation)
    context["stopped"] = bool(stopped)
    context["is_sun_cal"] = context["obs_type"].name == SUN_CAL_TYPE_NAME
    context["station"] = default_station(session)
    return TEMPLATES.TemplateResponse(request, "observation_detail.html", context)


@router.post("/observations/{obs_id}/checklist/{state_id}", response_class=HTMLResponse)
async def checklist_tick(
    request: Request, session: SessionDep, obs_id: int, state_id: int, view: str = "detail"
) -> HTMLResponse:
    """Tick/untick one checklist item (htmx) and re-render the checklist fragment.

    The checkbox submits ``checked`` only when ticked; ``by`` comes from the
    page's observer picker (``hx-include``). Who + when persist on the
    :class:`ChecklistItemState` row.
    """
    observation = get_or_404(session, Observation, obs_id)
    state = get_or_404(session, ChecklistItemState, state_id)
    if state.observation_id != observation.id:
        raise HTTPException(status_code=404, detail="checklist item not in this observation")
    form = await request.form()
    if "checked" in form:
        state.checked = True
        state.checked_at = utcnow()
        state.checked_by = str(form.get("by", ""))
    else:
        state.checked = False
        state.checked_at = None
        state.checked_by = ""
    session.add(state)
    session.commit()
    context = _detail_context(session, observation)
    context["view"] = view
    return TEMPLATES.TemplateResponse(request, "_checklist.html", context)


@router.post("/observations/{obs_id}/notes", response_class=HTMLResponse)
def notes_save(
    request: Request, session: SessionDep, obs_id: int, notes: Annotated[str, Form()] = ""
) -> HTMLResponse:
    """Save the notes editor content (htmx) and re-render the notes fragment."""
    observation = get_or_404(session, Observation, obs_id)
    observation.notes = notes
    observation.updated_at = utcnow()
    session.add(observation)
    session.commit()
    return TEMPLATES.TemplateResponse(
        request, "_notes.html", {"obs": observation, "saved_at": utcnow()}
    )


@router.post("/observations/{obs_id}/start")
def observation_start(session: SessionDep, obs_id: int) -> RedirectResponse:
    """Start a planned observation; 409 while required checklist items are unticked."""
    observation = get_or_404(session, Observation, obs_id)
    start_observation(session, observation)
    return _see_other(f"/observations/{obs_id}")


@router.post("/observations/{obs_id}/stop")
def observation_stop(session: SessionDep, obs_id: int) -> RedirectResponse:
    """Stop a running observation: ``actual_end`` now, status ``done`` (plan §5.3)."""
    observation = get_or_404(session, Observation, obs_id)
    if observation.status != "running":
        raise HTTPException(
            status_code=409, detail=f"cannot stop a {observation.status!r} observation"
        )
    observation.status = "done"
    observation.actual_end = utcnow()
    observation.updated_at = utcnow()
    session.add(observation)
    session.commit()
    return _see_other(f"/observations/{obs_id}?stopped=1")


@router.post("/observations/{obs_id}/abort")
def observation_abort(session: SessionDep, obs_id: int) -> RedirectResponse:
    """Abort a planned or running observation."""
    observation = get_or_404(session, Observation, obs_id)
    if observation.status not in ("planned", "running"):
        raise HTTPException(
            status_code=409, detail=f"cannot abort a {observation.status!r} observation"
        )
    observation.status = "aborted"
    if observation.actual_start is not None and observation.actual_end is None:
        observation.actual_end = utcnow()
    observation.updated_at = utcnow()
    session.add(observation)
    session.commit()
    return _see_other(f"/observations/{obs_id}")


@router.post("/observations/{obs_id}/suncal")
def suncal_offsets(
    session: SessionDep,
    obs_id: int,
    offset_az_deg: Annotated[float, Form()],
    offset_el_deg: Annotated[float, Form()],
) -> RedirectResponse:
    """Write the Sun-cal Δaz/Δel (scale/gauge minus predicted) to the Station.

    Only valid on a completed Sun pointing calibration observation. The old
    offsets are recorded in an automatically appended observation note
    (plan §5.4).
    """
    observation = get_or_404(session, Observation, obs_id)
    obs_type = get_or_404(session, ObservationType, observation.observation_type_id)
    if obs_type.name != SUN_CAL_TYPE_NAME:
        raise HTTPException(status_code=409, detail="not a Sun pointing calibration observation")
    if observation.status != "done":
        raise HTTPException(status_code=409, detail="observation is not completed")
    station = default_station(session)
    old_az, old_el = station.pointing_offset_az_deg, station.pointing_offset_el_deg
    station.pointing_offset_az_deg = offset_az_deg
    station.pointing_offset_el_deg = offset_el_deg
    station.updated_at = utcnow()
    _append_note(
        observation,
        f"pointing offsets updated az={offset_az_deg:+.2f}° el={offset_el_deg:+.2f}° "
        f"(was az={old_az:+.2f}° el={old_el:+.2f}°)",
    )
    session.add(station)
    session.add(observation)
    session.commit()
    return _see_other(f"/observations/{obs_id}")


# ---- JSON API (the M3 MCP surfaces, plan §12.4) ----------------------------------


class NoteBody(BaseModel):
    """Body for ``POST /api/observations/{id}/notes`` — a paragraph to append."""

    text: str


class TickBody(BaseModel):
    """Body for ``POST /api/observations/{id}/checklist/{item_id}/tick``."""

    by: str


def _observation_summary(
    observation: Observation, types: dict[int | None, str], sources: dict[int | None, str]
) -> dict[str, Any]:
    """One row of ``GET /api/observations``."""
    return {
        "id": observation.id,
        "name": observation.name,
        "status": observation.status,
        "type": types.get(observation.observation_type_id),
        "source": sources.get(observation.source_id),
        "actual_start": observation.actual_start,
        "actual_end": observation.actual_end,
        "created_at": observation.created_at,
    }


@router.get("/api/observations")
def api_observations(session: SessionDep) -> list[dict[str, Any]]:
    """Active observations, newest first (mirror of the list page). Archived
    (soft-deleted) observations are excluded — they are HTML-only and never
    exposed over MCP (roadmap M6)."""
    observations = session.exec(
        select(Observation)
        .where(col(Observation.archived_at).is_(None))
        .order_by(col(Observation.created_at).desc(), col(Observation.id).desc())
    ).all()
    types = {t.id: t.name for t in session.exec(select(ObservationType)).all()}
    sources = {s.id: s.name for s in session.exec(select(RadioSource)).all()}
    return [_observation_summary(o, types, sources) for o in observations]


@router.get("/api/observations/{obs_id}")
def api_observation(session: SessionDep, obs_id: int) -> dict[str, Any]:
    """One observation in full (mirror of the detail page)."""
    observation = get_or_404(session, Observation, obs_id)
    context = _detail_context(session, observation)
    return {
        **observation.model_dump(),
        "type": context["obs_type"].name,
        "source": context["source"].model_dump(),
        "location": context["location"].model_dump(),
        "observers": [o.name for o in context["observers"]],
        "checklist": [
            {
                "state_id": state.id,
                "text": item.text,
                "required": item.required,
                "checked": state.checked,
                "checked_at": state.checked_at,
                "checked_by": state.checked_by,
            }
            for state, item in context["checklist"]
        ],
        "required_ok": context["required_ok"],
        "captures": [c.model_dump() for c in context["captures"]],
    }


@router.post("/api/observations/{obs_id}/notes")
def api_append_note(session: SessionDep, obs_id: int, body: NoteBody) -> dict[str, Any]:
    """Append a paragraph to the observation notes; returns the full notes."""
    observation = get_or_404(session, Observation, obs_id)
    _append_note(observation, body.text)
    session.add(observation)
    session.commit()
    return {"id": observation.id, "notes": observation.notes}


@router.post("/api/observations/{obs_id}/checklist/{state_id}/tick")
def api_checklist_tick(
    session: SessionDep, obs_id: int, state_id: int, body: TickBody
) -> dict[str, Any]:
    """Tick one checklist item, recording who and when."""
    observation = get_or_404(session, Observation, obs_id)
    state = get_or_404(session, ChecklistItemState, state_id)
    if state.observation_id != observation.id:
        raise HTTPException(status_code=404, detail="checklist item not in this observation")
    state.checked = True
    state.checked_at = utcnow()
    state.checked_by = body.by
    session.add(state)
    session.commit()
    session.refresh(state)  # commit expires the row; reload before dumping
    return state.model_dump()


@router.get("/api/sources")
def api_sources(session: SessionDep) -> list[dict[str, Any]]:
    """All radio sources."""
    sources = session.exec(select(RadioSource).order_by(RadioSource.name)).all()
    return [s.model_dump() for s in sources]


@router.get("/api/whats_up")
def api_whats_up(
    session: SessionDep, window_h: Annotated[float, Query(gt=0, le=48)] = 8.0
) -> list[dict[str, Any]]:
    """Every source's current pointing + transit + beam-crossing, by elevation.

    Station Δaz/Δel offsets are applied; ``transit_within_window`` flags
    sources transiting within the next ``window_h`` hours (plan §12.4
    ``whats_up``).
    """
    from datetime import datetime, timedelta

    station = default_station(session)
    location = default_location(session)
    now = utcnow()
    entries: list[dict[str, Any]] = []
    for source in session.exec(select(RadioSource)).all():
        entry = source_pointing(source, location, station, when=now)
        transit = datetime.fromisoformat(entry["transit_utc"])
        entry["window_h"] = window_h
        entry["transit_within_window"] = transit <= now + timedelta(hours=window_h)
        entries.append(entry)
    entries.sort(key=lambda e: e["el_deg"], reverse=True)
    return entries


@router.get("/api/pointing/{source_id}")
def api_pointing(session: SessionDep, source_id: int) -> dict[str, Any]:
    """Current pointing block for one source (offsets applied, raw alongside)."""
    source = get_or_404(session, RadioSource, source_id)
    return source_pointing(source, default_location(session), default_station(session))
