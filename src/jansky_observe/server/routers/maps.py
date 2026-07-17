"""HI sky maps (roadmap M11): raster/drift maps of extended emission.

A :class:`~jansky_observe.models.SkyMap` groups pointed captures into a grid that
the reduction (:mod:`jansky_observe.confirm.mapping`) bins into a coarse 2-D map —
beam-limited to the dish's ~21° HPBW. Captures arrive from the az/el raster runner
(:mod:`jansky_observe.server.mapping`, which slews through
:func:`jansky_observe.server.rotator.slew`) or by ingesting a drift-scan
:class:`~jansky_observe.models.Campaign`. All verbs are HTML-only except the
read-only JSON/image endpoints (plan §12.4); the raster reaches hardware only
through M9's guarded slew primitive.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from sqlmodel import col, select

from jansky_observe.astro.pointing import hpbw_deg
from jansky_observe.export.figures import sky_map_figure
from jansky_observe.models import (
    SKY_MAP_FRAMES,
    SKY_MAP_METRICS,
    Campaign,
    Capture,
    RadioSource,
    SkyMap,
)
from jansky_observe.server.mapping import (
    MapRunState,
    build_sky_map,
    map_cells,
    map_summary,
    start_map,
    stop_map,
)
from jansky_observe.server.routers import (
    TEMPLATES,
    SessionDep,
    default_location,
    default_station,
    get_or_404,
)

__all__ = ["router"]

router = APIRouter(tags=["maps"])


def _run_state(request: Request, map_id: int) -> dict[str, Any] | None:
    """The raster runner's state for this map, if it's the one running."""
    state: MapRunState = getattr(request.app.state, "mapping", MapRunState())
    if state.active_map_id != map_id:
        return None
    return {
        "cells_total": state.cells_total,
        "cells_done": state.cells_done,
        "note": state.note,
    }


@router.get("/maps", response_class=HTMLResponse)
def maps_page(request: Request, session: SessionDep) -> HTMLResponse:
    """All sky maps (active first) + a create form."""
    maps = session.exec(select(SkyMap).order_by(col(SkyMap.status), col(SkyMap.id).desc())).all()
    sources = session.exec(select(RadioSource).order_by(col(RadioSource.name))).all()
    station = default_station(session)
    location = default_location(session)
    rows = [
        {"map": m, "summary": map_summary(session, m, station, location)}
        for m in maps
        if m.id is not None
    ]
    return TEMPLATES.TemplateResponse(
        request,
        "maps.html",
        {
            "rows": rows,
            "sources": sources,
            "frames": SKY_MAP_FRAMES,
            "metrics": SKY_MAP_METRICS,
        },
    )


@router.post("/maps")
def create_map(
    session: SessionDep,
    name: Annotated[str, Form()],
    frame: Annotated[str, Form()] = "galactic",
    metric: Annotated[str, Form()] = "hi_intensity",
    source_id: Annotated[int | None, Form()] = None,
    center_x_deg: Annotated[float, Form()] = 0.0,
    center_y_deg: Annotated[float, Form()] = 0.0,
    extent_x_deg: Annotated[float, Form()] = 60.0,
    extent_y_deg: Annotated[float, Form()] = 60.0,
    step_deg: Annotated[float, Form()] = 10.0,
    dwell_s: Annotated[float, Form()] = 60.0,
    notes: Annotated[str, Form()] = "",
) -> RedirectResponse:
    """Create a sky map (active). HTML-only."""
    if frame not in SKY_MAP_FRAMES:
        raise HTTPException(status_code=422, detail=f"frame must be one of {SKY_MAP_FRAMES}")
    if metric not in SKY_MAP_METRICS:
        raise HTTPException(status_code=422, detail=f"metric must be one of {SKY_MAP_METRICS}")
    if step_deg <= 0:
        raise HTTPException(status_code=422, detail="step_deg must be positive")
    if source_id:
        get_or_404(session, RadioSource, source_id)
    sky_map = SkyMap(
        name=name.strip(),
        source_id=source_id or None,
        frame=frame,
        metric=metric,
        center_x_deg=center_x_deg,
        center_y_deg=center_y_deg,
        extent_x_deg=extent_x_deg,
        extent_y_deg=extent_y_deg,
        step_deg=step_deg,
        dwell_s=dwell_s,
        notes=notes.strip(),
        status="active",
    )
    session.add(sky_map)
    session.commit()
    session.refresh(sky_map)
    return RedirectResponse(f"/maps/{sky_map.id}", status_code=303)


@router.post("/maps/{map_id}/status")
def set_map_status(
    session: SessionDep, map_id: int, status: Annotated[str, Form()]
) -> RedirectResponse:
    """Activate or close a map. HTML-only."""
    if status not in ("active", "done"):
        raise HTTPException(status_code=422, detail="status must be 'active' or 'done'")
    sky_map = get_or_404(session, SkyMap, map_id)
    sky_map.status = status
    session.add(sky_map)
    session.commit()
    return RedirectResponse(f"/maps/{map_id}", status_code=303)


@router.post("/maps/{map_id}/start")
def start_raster(request: Request, session: SessionDep, map_id: int) -> RedirectResponse:
    """Begin rastering an az/el map with the rotator. HTML-only.

    409 unless the map is an active ``azel``-frame map and a rotator is
    configured — galactic maps are filled by ingest, not the raster.
    """
    sky_map = get_or_404(session, SkyMap, map_id)
    if sky_map.frame != "azel":
        raise HTTPException(
            status_code=409,
            detail="the raster runs in az/el; use an azel-frame map (galactic maps ingest drift passes)",
        )
    if sky_map.status != "active":
        raise HTTPException(status_code=409, detail="map is not active")
    station = default_station(session)
    if station.rotator_kind == "none":
        raise HTTPException(status_code=409, detail="no rotator configured on this station")
    start_map(request.app, map_id, len(map_cells(sky_map)))
    return RedirectResponse(f"/maps/{map_id}", status_code=303)


@router.post("/maps/{map_id}/stop")
def stop_raster(request: Request, session: SessionDep, map_id: int) -> RedirectResponse:
    """Stop the raster. HTML-only."""
    get_or_404(session, SkyMap, map_id)
    stop_map(request.app)
    return RedirectResponse(f"/maps/{map_id}", status_code=303)


@router.post("/maps/{map_id}/ingest")
def ingest_campaign(
    session: SessionDep, map_id: int, campaign_id: Annotated[int, Form()]
) -> RedirectResponse:
    """Ingest a drift-scan campaign's captures into this map (no hardware).

    Each capture is tagged with the map id and the campaign's fixed az/el (the
    beam pointing while the sky drifted); the reduce path converts each to l/b at
    its own time. 422 if the campaign has no fixed az/el. HTML-only.
    """
    sky_map = get_or_404(session, SkyMap, map_id)
    campaign = get_or_404(session, Campaign, campaign_id)
    if campaign.fixed_az_deg is None or campaign.fixed_el_deg is None:
        raise HTTPException(status_code=422, detail="campaign has no fixed az/el to map from")
    captures = session.exec(select(Capture).where(Capture.campaign_id == campaign_id)).all()
    for capture in captures:
        capture.sky_map_id = sky_map.id
        capture.map_az_deg = campaign.fixed_az_deg
        capture.map_el_deg = campaign.fixed_el_deg
        session.add(capture)
    session.commit()
    return RedirectResponse(f"/maps/{map_id}", status_code=303)


@router.get("/maps/{map_id}", response_class=HTMLResponse)
def map_detail(request: Request, session: SessionDep, map_id: int) -> HTMLResponse:
    """One map: the rendered heatmap, coverage, controls, and contributing captures."""
    sky_map = get_or_404(session, SkyMap, map_id)
    station = default_station(session)
    location = default_location(session)
    summary = map_summary(session, sky_map, station, location)
    campaigns = session.exec(select(Campaign).where(col(Campaign.fixed_az_deg).is_not(None))).all()
    return TEMPLATES.TemplateResponse(
        request,
        "map_detail.html",
        {
            "map": sky_map,
            "summary": summary,
            "run": _run_state(request, map_id),
            "campaigns": campaigns,
            "metrics": SKY_MAP_METRICS,
            "rotator_configured": station.rotator_kind != "none",
        },
    )


@router.get("/api/maps")
def api_maps(request: Request, session: SessionDep) -> list[dict[str, Any]]:
    """All sky maps (active first), each with geometry, coverage, and grid."""
    station = default_station(session)
    location = default_location(session)
    maps = session.exec(select(SkyMap).order_by(col(SkyMap.status), col(SkyMap.id).desc())).all()
    out = []
    for sky_map in maps:
        summary = map_summary(session, sky_map, station, location)
        summary["run"] = _run_state(request, sky_map.id) if sky_map.id else None
        out.append(summary)
    return out


@router.get("/api/maps/{map_id}")
def api_map(request: Request, session: SessionDep, map_id: int) -> dict[str, Any]:
    """One map with its geometry, coverage, gridded values, and raster state."""
    sky_map = get_or_404(session, SkyMap, map_id)
    station = default_station(session)
    location = default_location(session)
    summary = map_summary(session, sky_map, station, location)
    summary["run"] = _run_state(request, map_id)
    return summary


@router.get("/api/maps/{map_id}/image.png")
def api_map_image(
    request: Request, session: SessionDep, map_id: int, metric: str | None = None
) -> FileResponse:
    """The rendered heatmap PNG (beam-limited). ``metric`` overrides the map's default."""
    sky_map = get_or_404(session, SkyMap, map_id)
    if metric is not None and metric not in SKY_MAP_METRICS:
        raise HTTPException(status_code=422, detail=f"metric must be one of {SKY_MAP_METRICS}")
    use_metric = metric or sky_map.metric
    station = default_station(session)
    location = default_location(session)
    gridded = build_sky_map(session, sky_map, station, location, use_metric)
    out = Path(request.app.state.settings.data_dir) / "plots" / f"skymap-{map_id}-{use_metric}.png"
    sky_map_figure(
        gridded,
        out,
        frame=sky_map.frame,
        metric=use_metric,
        hpbw_deg=hpbw_deg(station.dish_diameter_m),
        title=f"{sky_map.name} — {use_metric} ({sky_map.frame})",
    )
    return FileResponse(out, media_type="image/png")
