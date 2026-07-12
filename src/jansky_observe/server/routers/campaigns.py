"""Drift-scan campaigns (roadmap M7, plan 80).

A :class:`~jansky_observe.models.Campaign` groups fixed-pointing drift-scan
passes taken over many nights. Captures registered while a campaign is *active*
are tagged with its id and a sidereal-day number
(:func:`~jansky_observe.astro.pointing.sidereal_day_number`); passes at the same
LST on different sidereal days stack. All verbs are HTML-only (plan §12.4).
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlmodel import Session, col, select

from jansky_observe.astro.pointing import local_sidereal_time_hours, sidereal_day_number
from jansky_observe.models import Campaign, Capture, RadioSource
from jansky_observe.server.routers import (
    TEMPLATES,
    SessionDep,
    default_location,
    get_or_404,
)

__all__ = ["campaign_passes", "router"]

router = APIRouter(tags=["campaigns"])


def campaign_passes(session: Session, campaign_id: int) -> list[dict[str, Any]]:
    """A campaign's captures grouped into passes by sidereal day, newest pass
    first; each capture carries its LST at start (the drift-scan phase used to
    stack across days)."""
    lon = default_location(session).lon_deg
    captures = session.exec(
        select(Capture)
        .where(Capture.campaign_id == campaign_id)
        .order_by(col(Capture.sidereal_day).desc(), col(Capture.id))
    ).all()
    passes: dict[int, list[dict[str, Any]]] = {}
    for capture in captures:
        day = capture.sidereal_day if capture.sidereal_day is not None else -1
        lst = local_sidereal_time_hours(lon, capture.start) if capture.start else None
        passes.setdefault(day, []).append(
            {"id": capture.id, "start": capture.start, "lst_hours": lst, "kind": capture.kind}
        )
    return [
        {"sidereal_day": day, "captures": caps}
        for day, caps in sorted(passes.items(), reverse=True)
    ]


def _campaign_summary(session: Session, campaign: Campaign) -> dict[str, Any]:
    source = session.get(RadioSource, campaign.source_id)
    passes = campaign_passes(session, campaign.id) if campaign.id is not None else []
    return {
        "id": campaign.id,
        "name": campaign.name,
        "source": None if source is None else source.name,
        "status": campaign.status,
        "fixed_az_deg": campaign.fixed_az_deg,
        "fixed_el_deg": campaign.fixed_el_deg,
        "notes": campaign.notes,
        "n_passes": len(passes),
        "n_captures": sum(len(p["captures"]) for p in passes),
    }


@router.get("/campaigns", response_class=HTMLResponse)
def campaigns_page(request: Request, session: SessionDep) -> HTMLResponse:
    """Drift-scan campaigns (active first) + a create form."""
    campaigns = session.exec(
        select(Campaign).order_by(col(Campaign.status), col(Campaign.id).desc())
    ).all()
    sources = session.exec(select(RadioSource).order_by(col(RadioSource.name))).all()
    rows = [
        {"campaign": c, "summary": _campaign_summary(session, c)}
        for c in campaigns
        if c.id is not None
    ]
    return TEMPLATES.TemplateResponse(request, "campaigns.html", {"rows": rows, "sources": sources})


@router.post("/campaigns")
def create_campaign(
    session: SessionDep,
    name: Annotated[str, Form()],
    source_id: Annotated[int, Form()],
    fixed_az_deg: Annotated[float | None, Form()] = None,
    fixed_el_deg: Annotated[float | None, Form()] = None,
    notes: Annotated[str, Form()] = "",
) -> RedirectResponse:
    """Create a drift-scan campaign (active). HTML-only."""
    get_or_404(session, RadioSource, source_id)
    campaign = Campaign(
        name=name.strip(),
        source_id=source_id,
        fixed_az_deg=fixed_az_deg,
        fixed_el_deg=fixed_el_deg,
        notes=notes.strip(),
        status="active",
    )
    session.add(campaign)
    session.commit()
    session.refresh(campaign)
    return RedirectResponse(f"/campaigns/{campaign.id}", status_code=303)


@router.post("/campaigns/{campaign_id}/status")
def set_campaign_status(
    session: SessionDep, campaign_id: int, status: Annotated[str, Form()]
) -> RedirectResponse:
    """Activate or close a campaign. HTML-only."""
    if status not in ("active", "done"):
        raise HTTPException(status_code=422, detail="status must be 'active' or 'done'")
    campaign = get_or_404(session, Campaign, campaign_id)
    campaign.status = status
    session.add(campaign)
    session.commit()
    return RedirectResponse(f"/campaigns/{campaign_id}", status_code=303)


@router.post("/captures/{capture_id}/campaign")
def set_capture_campaign(
    session: SessionDep, capture_id: int, campaign_id: Annotated[int, Form()]
) -> RedirectResponse:
    """Attach a capture to a campaign (0 = detach), tagging its sidereal day.
    HTML-only."""
    capture = get_or_404(session, Capture, capture_id)
    if campaign_id == 0:
        capture.campaign_id = None
        capture.sidereal_day = None
    else:
        get_or_404(session, Campaign, campaign_id)
        lon = default_location(session).lon_deg
        capture.campaign_id = campaign_id
        capture.sidereal_day = sidereal_day_number(lon, capture.start)
    session.add(capture)
    session.commit()
    dest = f"/observations/{capture.observation_id}" if capture.observation_id else "/campaigns"
    return RedirectResponse(dest, status_code=303)


@router.get("/campaigns/{campaign_id}", response_class=HTMLResponse)
def campaign_detail(request: Request, session: SessionDep, campaign_id: int) -> HTMLResponse:
    """One campaign: its passes (grouped by sidereal day) and captures with LST."""
    campaign = get_or_404(session, Campaign, campaign_id)
    return TEMPLATES.TemplateResponse(
        request,
        "campaign_detail.html",
        {
            "campaign": campaign,
            "summary": _campaign_summary(session, campaign),
            "passes": campaign_passes(session, campaign_id),
        },
    )


@router.get("/api/campaigns")
def api_campaigns(session: SessionDep) -> list[dict[str, Any]]:
    """All drift-scan campaigns (active first), with pass/capture counts."""
    campaigns = session.exec(
        select(Campaign).order_by(col(Campaign.status), col(Campaign.id).desc())
    ).all()
    return [_campaign_summary(session, c) for c in campaigns]


@router.get("/api/campaigns/{campaign_id}")
def api_campaign(session: SessionDep, campaign_id: int) -> dict[str, Any]:
    """One campaign with its passes (grouped by sidereal day, captures + LST)."""
    campaign = get_or_404(session, Campaign, campaign_id)
    summary = _campaign_summary(session, campaign)
    summary["passes"] = campaign_passes(session, campaign_id)
    return summary
