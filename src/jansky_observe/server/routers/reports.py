"""Report + export endpoints (plan §7, §4.7 — M4).

HTML: ``POST /observations/{id}/report`` builds (or rebuilds) the PDF —
htmx-friendly: an ``HX-Request`` gets back a small fragment linking to the
file, a plain form POST gets a redirect to it. ``GET
/observations/{id}/report.pdf`` serves the built file (404 until built).

JSON: ``POST /api/observations/{id}/report`` returns ``{"path": ...}`` (the
MCP ``build_report`` verb proxies this), and ``GET
/api/captures/{id}/export?format=virgo_csv|ezra_txt`` builds a one-way
spectrum export under ``<data_dir>/exports/`` and returns it as a download
(plan §4.7 — ``.npz`` captures only; the ezRA header needs the linked
observation's pointing and location, 409 with detail when absent).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from jansky_observe.export.bundle import build_observation_manifest, write_observation_bundle
from jansky_observe.export.ezra_txt import export_ezra_txt
from jansky_observe.export.pdf import build_report, report_path
from jansky_observe.export.virgo_csv import export_virgo_csv
from jansky_observe.models import Capture, Location, Observation, Station
from jansky_observe.server.routers import SessionDep, get_or_404

__all__ = ["router"]

router = APIRouter(tags=["reports"])

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"


def _build(request: Request, session: SessionDep, obs_id: int) -> Path:
    """Build the observation's report; 404 for an unknown observation."""
    get_or_404(session, Observation, obs_id)
    return build_report(
        request.app.state.engine, obs_id, request.app.state.settings.data_dir, _TEMPLATES_DIR
    )


# ---- HTML (the observation detail page's report button) --------------------------


@router.post("/observations/{obs_id}/report", response_class=HTMLResponse, response_model=None)
def observation_report_build(
    request: Request, session: SessionDep, obs_id: int
) -> HTMLResponse | RedirectResponse:
    """Build (or rebuild) the PDF report (plan §7).

    htmx requests get a fragment with the download link; plain form POSTs
    are redirected straight to the built PDF.
    """
    _build(request, session, obs_id)
    if request.headers.get("hx-request"):
        return HTMLResponse(
            f'<a class="btn" href="/observations/{obs_id}/report.pdf">Download report.pdf</a> '
            '<span class="muted">rebuilt</span>'
        )
    return RedirectResponse(f"/observations/{obs_id}/report.pdf", status_code=303)


@router.get("/observations/{obs_id}/report.pdf")
def observation_report_pdf(request: Request, session: SessionDep, obs_id: int) -> FileResponse:
    """The built report PDF; 404 until a build has run."""
    get_or_404(session, Observation, obs_id)
    path = report_path(request.app.state.settings.data_dir, obs_id)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="no report built for this observation yet")
    return FileResponse(path, media_type="application/pdf")


# ---- JSON API (the MCP export verbs, plan §12.4) ----------------------------------


@router.post("/api/observations/{obs_id}/report")
def api_observation_report(request: Request, session: SessionDep, obs_id: int) -> dict[str, Any]:
    """Build (or rebuild) the PDF report; returns the written path."""
    return {"path": str(_build(request, session, obs_id))}


@router.get("/api/observations/{obs_id}/bundle.json")
def api_observation_bundle_manifest(session: SessionDep, obs_id: int) -> dict[str, Any]:
    """The observation bundle manifest (roadmap M8, plan 78 input format): station
    UUID, pointing, LST, timestamps, gain, cal-epoch, and classifier verdicts for
    every capture. The machine-readable half of the bundle — the ``get_observation
    _bundle`` MCP tool proxies this; the ``.npz`` spectra ride the zip download."""
    observation = get_or_404(session, Observation, obs_id)
    return build_observation_manifest(session, observation)


@router.get("/api/observations/{obs_id}/bundle")
def api_observation_bundle(request: Request, session: SessionDep, obs_id: int) -> FileResponse:
    """The full observation bundle as a zip download (roadmap M8, plan 78): a
    ``bundle.json`` manifest plus one self-describing averaged-spectrum
    ``capture-<id>.npz`` per on-disk npz capture. Built under
    ``<data_dir>/exports/`` and overwritten on re-export."""
    observation = get_or_404(session, Observation, obs_id)
    exports_dir = Path(request.app.state.settings.data_dir) / "exports"
    zip_path = write_observation_bundle(session, observation, exports_dir)
    return FileResponse(
        zip_path, media_type="application/zip", filename=zip_path.name
    )


def _ezra_context(session: SessionDep, capture: Capture) -> dict[str, Any]:
    """The ezRA header kwargs from the capture's linked observation.

    Raises
    ------
    HTTPException
        409 when the capture has no linked observation, the observation has
        no location, or neither a dialed nor a computed pointing was
        recorded — the ezRA header cannot be written honestly without them.
    """

    def _conflict(reason: str) -> HTTPException:
        return HTTPException(
            status_code=409, detail=f"ezRA export needs {reason} (plan §4.7 header fields)"
        )

    if capture.observation_id is None:
        raise _conflict("a linked observation for pointing and location")
    observation = session.get(Observation, capture.observation_id)
    if observation is None:
        raise _conflict("a linked observation for pointing and location")
    location = session.get(Location, observation.location_id)
    if location is None:
        raise _conflict("the observation's location")
    azimuth = (
        observation.pointing_az_deg
        if observation.pointing_az_deg is not None
        else observation.computed_az_deg
    )
    elevation = (
        observation.pointing_el_deg
        if observation.pointing_el_deg is not None
        else observation.computed_el_deg
    )
    if azimuth is None or elevation is None:
        raise _conflict("a recorded az/el pointing on the observation")
    station = session.get(Station, observation.station_id)
    return {
        "lat": location.lat_deg,
        "lon": location.lon_deg,
        "elev": location.elevation_m,
        "azimuth_deg": azimuth,
        "elevation_deg": elevation,
        "name": station.name if station is not None else "jansky-observe",
    }


@router.get("/api/captures/{capture_id}/export")
def api_capture_export(
    request: Request,
    session: SessionDep,
    capture_id: int,
    format: Literal["virgo_csv", "ezra_txt"],
) -> FileResponse:
    """Export a capture's averaged spectrum as a download (plan §4.7).

    ``virgo_csv`` needs only the capture file; ``ezra_txt`` also needs the
    linked observation's pointing and location for its header — 409 with
    detail when absent. ``.npz`` captures only (409 otherwise); files land
    under ``<data_dir>/exports/`` and are overwritten on re-export.
    """
    capture = get_or_404(session, Capture, capture_id)
    if capture.format != "npz_spectra":
        raise HTTPException(
            status_code=409,
            detail=f"capture format {capture.format!r} not exportable (npz_spectra only)",
        )
    exports_dir = Path(request.app.state.settings.data_dir) / "exports"
    try:
        if format == "virgo_csv":
            out = export_virgo_csv(capture.path, exports_dir / f"capture-{capture_id}-virgo.csv")
            media_type = "text/csv"
        else:
            context = _ezra_context(session, capture)
            out = export_ezra_txt(
                capture.path, exports_dir / f"capture-{capture_id}-ezra.txt", **context
            )
            media_type = "text/plain"
    except FileNotFoundError:
        raise HTTPException(
            status_code=404, detail=f"capture file missing: {capture.path}"
        ) from None
    return FileResponse(out, media_type=media_type, filename=out.name)
