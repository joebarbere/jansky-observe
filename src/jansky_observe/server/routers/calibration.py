"""Calibration captures + epochs (roadmap M7, plans 78/79).

A :class:`~jansky_observe.models.CalibrationEpoch` records "the receiver was
freshly calibrated as of this time." The operator marks captures as calibration
kinds (ref load / cold sky / hot ground) and attaches them to the current epoch;
every science capture is stamped with the epoch in effect when it was taken
(:func:`~jansky_observe.server.routers.captures.latest_cal_epoch_id`).

All the verbs here are HTML-only — the MCP surface stays read-mostly (plan
§12.4); calibration state is visible through the existing capture-meta reads.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlmodel import Session, col, select

from jansky_observe.confirm.classifier import averaged_spectrum
from jansky_observe.confirm.skyground import sky_ground_delta
from jansky_observe.models import (
    CALIBRATION_KINDS,
    CAPTURE_KINDS,
    CalibrationEpoch,
    Capture,
    utcnow,
)
from jansky_observe.server.routers import TEMPLATES, SessionDep, get_or_404

__all__ = ["epoch_captures", "latest_epoch", "router"]

router = APIRouter(tags=["calibration"])


def latest_epoch(session: Session) -> CalibrationEpoch | None:
    """The most recent calibration epoch, or ``None``."""
    return session.exec(
        select(CalibrationEpoch).order_by(col(CalibrationEpoch.started_at).desc())
    ).first()


def epoch_captures(session: Session, epoch_id: int) -> dict[str, list[Capture]]:
    """A calibration epoch's captures grouped by kind (ref/cold/hot)."""
    rows = session.exec(
        select(Capture)
        .where(Capture.cal_epoch_id == epoch_id)
        .where(col(Capture.kind).in_(CALIBRATION_KINDS))
        .order_by(col(Capture.id))
    ).all()
    grouped: dict[str, list[Capture]] = {kind: [] for kind in CALIBRATION_KINDS}
    for capture in rows:
        grouped.setdefault(capture.kind, []).append(capture)
    return grouped


def _epoch_summary(session: Session, epoch: CalibrationEpoch) -> dict[str, Any]:
    grouped = epoch_captures(session, epoch.id) if epoch.id is not None else {}
    return {
        "id": epoch.id,
        "started_at": epoch.started_at,
        "notes": epoch.notes,
        "captures": {kind: [c.id for c in caps] for kind, caps in grouped.items()},
        "complete": all(grouped.get(kind) for kind in CALIBRATION_KINDS),
        "sky_ground_delta_db": epoch.sky_ground_delta_db,
        "tsys_k": epoch.tsys_k,
    }


@router.get("/calibration", response_class=HTMLResponse)
def calibration_page(request: Request, session: SessionDep) -> HTMLResponse:
    """Calibration epochs, newest first, with their captures grouped by kind."""
    epochs = session.exec(
        select(CalibrationEpoch).order_by(col(CalibrationEpoch.started_at).desc())
    ).all()
    rows = [
        {
            "epoch": e,
            "captures": epoch_captures(session, e.id),
            "complete": _epoch_summary(session, e)["complete"],
        }
        for e in epochs
        if e.id is not None
    ]
    return TEMPLATES.TemplateResponse(
        request,
        "calibration.html",
        {"rows": rows, "calibration_kinds": CALIBRATION_KINDS},
    )


@router.post("/calibration/epochs")
def create_epoch(session: SessionDep, notes: Annotated[str, Form()] = "") -> RedirectResponse:
    """Start a new calibration epoch (started_at = now). HTML-only."""
    epoch = CalibrationEpoch(started_at=utcnow(), notes=notes.strip())
    session.add(epoch)
    session.commit()
    return RedirectResponse("/calibration", status_code=303)


@router.post("/calibration/epochs/{epoch_id}/tsys")
def compute_epoch_tsys(session: SessionDep, epoch_id: int) -> RedirectResponse:
    """Compute the sky/ground ΔdB + Tsys for a calibration epoch (roadmap M10).

    Loads the epoch's ``cold_sky`` and ``hot_ground`` captures, runs the Y-factor
    reduction (:func:`~jansky_observe.confirm.skyground.sky_ground_delta`) over
    their averaged spectra, and stores ``sky_ground_delta_db`` / ``tsys_k`` on the
    epoch — the runbook's trendable system-health number. HTML-only.

    422 if the epoch lacks a ``cold_sky`` or a ``hot_ground`` capture, if a
    capture file is purged or missing on disk, or if the pair is unphysical
    (Y-factor ≤ 1 — the ground band power must exceed the cold sky's).
    """
    epoch = get_or_404(session, CalibrationEpoch, epoch_id)
    grouped = epoch_captures(session, epoch_id)
    cold = grouped.get("cold_sky") or []
    hot = grouped.get("hot_ground") or []
    if not cold or not hot:
        raise HTTPException(
            status_code=422,
            detail="epoch needs both a cold_sky and a hot_ground capture to compute Tsys",
        )
    cold_cap, hot_cap = cold[0], hot[0]
    for cap in (cold_cap, hot_cap):
        if cap.purged_at is not None:
            raise HTTPException(
                status_code=422,
                detail=f"capture #{cap.id} ({cap.kind}) was purged — its file is gone",
            )
    try:
        _, cold_db = averaged_spectrum(cold_cap.path)
        _, hot_db = averaged_spectrum(hot_cap.path)
        result = sky_ground_delta(cold_db, hot_db)
    except (FileNotFoundError, OSError) as exc:
        raise HTTPException(status_code=422, detail=f"capture file missing on disk: {exc}") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    epoch.sky_ground_delta_db = result["delta_db"]
    epoch.tsys_k = result["tsys_k"]
    session.add(epoch)
    session.commit()
    return RedirectResponse("/calibration", status_code=303)


@router.post("/captures/{capture_id}/kind")
def set_capture_kind(
    session: SessionDep, capture_id: int, kind: Annotated[str, Form()]
) -> RedirectResponse:
    """Set a capture's measurement kind (roadmap M7). A calibration kind
    attaches the capture to the latest calibration epoch (409 if none exists);
    setting it back to ``science`` re-stamps it with the epoch in effect. HTML-only."""
    if kind not in CAPTURE_KINDS:
        raise HTTPException(status_code=422, detail=f"unknown capture kind {kind!r}")
    capture = get_or_404(session, Capture, capture_id)
    epoch = latest_epoch(session)
    if kind in CALIBRATION_KINDS:
        if epoch is None:
            raise HTTPException(
                status_code=409,
                detail="no calibration epoch — start one on the Calibration page first",
            )
        capture.cal_epoch_id = epoch.id
    else:  # science: stamp with the epoch in effect (the latest), like registration
        capture.cal_epoch_id = None if epoch is None else epoch.id
    capture.kind = kind
    session.add(capture)
    session.commit()
    dest = f"/observations/{capture.observation_id}" if capture.observation_id else "/calibration"
    return RedirectResponse(dest, status_code=303)


@router.get("/api/calibration_epochs")
def api_calibration_epochs(session: SessionDep) -> list[dict[str, Any]]:
    """Calibration epochs, newest first — id, started_at, notes, the calibration
    captures grouped by kind, and whether all three kinds are present."""
    epochs = session.exec(
        select(CalibrationEpoch).order_by(col(CalibrationEpoch.started_at).desc())
    ).all()
    return [_epoch_summary(session, e) for e in epochs]
