"""Capture registry + confirmation API (plan §5.3, §6, §12.4).

JSON: list/meta routes, the averaged spectrum with a MHz or v_LSR axis, the
deterministic ``hline_v1`` classify verb (each run appends a
:class:`~jansky_observe.models.ClassifierResult` row and renders a verdict
plot — provenance rule, plan §12.5), the result rows, the rendered plot PNG,
and an import verb that registers loose capture files found on disk.

HTML: the htmx classify fragment used by the observation detail page's
captures table.

Registration of a capture the daemon just stopped lives here too
(:func:`register_stopped_capture`) — ``server/app.py`` calls it from the
``/api/capture/stop`` handler. ``POST /api/rfi_sweep`` (plan §4.2/§5.2)
proxies the daemon's blocking HackRF sweep command and registers the CSV
via :func:`register_rfi_sweep_capture`.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import numpy as np
from astropy.coordinates import SkyCoord
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy import Engine
from sqlmodel import Session, col, select

from jansky_observe.astro.lsr import vlsr_axis
from jansky_observe.confirm.classifier import (
    CLASSIFIER_NAME,
    averaged_spectrum,
    classify_capture_npz,
)
from jansky_observe.confirm.plots import verdict_plot
from jansky_observe.control import ctl_request
from jansky_observe.models import (
    Capture,
    ClassifierResult,
    Location,
    Observation,
    RadioSource,
    utcnow,
)
from jansky_observe.server.live_badge import source_coord
from jansky_observe.server.routers import TEMPLATES, SessionDep, get_or_404

__all__ = [
    "parse_capture_settings",
    "register_rfi_sweep_capture",
    "register_stopped_capture",
    "router",
]

router = APIRouter(tags=["captures"])

_STATUS_FORMATS = {"npz": "npz_spectra", "sigmf": "sigmf"}
"""Daemon capture format → Capture.format value."""

# Same client-side-failure markers as server/app.py's _ctl closure: ctl_request
# never raises, so these substrings mean "daemon unreachable" → 503; any other
# ok=false reply is the daemon refusing the command → 409.
_UNREACHABLE_MARKERS = ("did not reply", "control channel error", "malformed reply")
_SWEEP_CTL_TIMEOUT_MS = 90_000
"""The rfi_sweep command blocks the daemon for the sweep's duration (seconds,
bounded by hackrf_sweep.SWEEP_TIMEOUT_S) — far past control.CTL_TIMEOUT_MS."""


# ---- registration (shared with the /api/capture/stop handler) --------------------


def parse_capture_settings(path: Path) -> dict[str, Any]:
    """Read the SDR settings recorded inside a capture file; ``{}`` on failure.

    ``.npz`` captures carry a JSON ``settings`` key
    (:class:`~jansky_observe.capture.writer.NpzCaptureWriter`); SigMF captures
    carry ``jansky_observe:settings`` in the ``<base>.sigmf-meta`` global
    (:class:`~jansky_observe.capture.writer.SigmfCaptureWriter`). Settings are
    advisory — a missing or malformed file degrades to ``{}``, never an error.
    """
    try:
        if path.suffix == ".npz":
            with np.load(path, allow_pickle=False) as data:
                parsed = json.loads(str(data["settings"]))
        else:
            meta = json.loads(path.with_suffix(".sigmf-meta").read_text())
            parsed = meta["global"]["jansky_observe:settings"]
        return dict(parsed) if isinstance(parsed, dict) else {}
    except Exception:  # noqa: BLE001 — settings are advisory; degrade, never block
        return {}


def _running_observation_id(session: Session) -> int | None:
    """The currently running observation's id, or ``None``."""
    observation = session.exec(
        select(Observation)
        .where(Observation.status == "running")
        .order_by(col(Observation.id).desc())
    ).first()
    return None if observation is None else observation.id


def register_stopped_capture(engine: Engine | None, status: dict[str, Any]) -> int | None:
    """Create a :class:`Capture` row for a capture the daemon just stopped.

    Parameters
    ----------
    engine : Engine or None
        The app's database engine; ``None`` (tests without a lifespan) skips
        registration.
    status : dict
        The daemon's stop reply — the *final* status of the stopped capture
        (format, path, bytes_written, elapsed_s, source).

    Returns
    -------
    int or None
        The new capture id, or ``None`` when the reply describes no capture.
    """
    fmt = _STATUS_FORMATS.get(str(status.get("format")))
    path = status.get("path")
    if engine is None or fmt is None or not path:
        return None
    ended = utcnow()
    started = ended - timedelta(seconds=float(status.get("elapsed_s") or 0.0))
    with Session(engine) as session:
        capture = Capture(
            observation_id=_running_observation_id(session),
            device=str(status.get("source", "unknown")),
            path=str(path),
            format=fmt,
            size_bytes=int(status.get("bytes_written") or 0),
            start=started,
            end=ended,
            sdr_settings=parse_capture_settings(Path(str(path))),
        )
        session.add(capture)
        session.commit()
        session.refresh(capture)
        return capture.id


# ---- RFI sweep (plan §4.2 survey mode, §5.2 button, §12.4 safe verb) ----------------


class RfiSweepBody(BaseModel):
    """Optional request body for ``POST /api/rfi_sweep``."""

    freq_lo_mhz: float = 1000
    freq_hi_mhz: float = 2000


def register_rfi_sweep_capture(engine: Engine | None, reply: dict[str, Any]) -> int | None:
    """Create a :class:`Capture` row for a finished RFI sweep.

    ``reply`` is the daemon's ``rfi_sweep`` success reply (path + summary).
    The raw CSV is the capture; the summary numbers ride along in
    ``sdr_settings`` for provenance. ``None`` when there is no engine (tests
    without a lifespan) or no path.
    """
    path = reply.get("path")
    if engine is None or not path:
        return None
    csv_path = Path(str(path))
    with Session(engine) as session:
        capture = Capture(
            observation_id=_running_observation_id(session),
            device="hackrf",
            path=str(path),
            format="hackrf_sweep_csv",
            size_bytes=csv_path.stat().st_size if csv_path.exists() else 0,
            end=utcnow(),
            sdr_settings={
                "freq_range_hz": reply.get("freq_range_hz"),
                "num_sweeps": reply.get("num_sweeps"),
                "n_rows": reply.get("n_rows"),
            },
        )
        session.add(capture)
        session.commit()
        session.refresh(capture)
        return capture.id


@router.post("/api/rfi_sweep")
async def api_rfi_sweep(request: Request, body: RfiSweepBody | None = None) -> dict[str, Any]:
    """Run a HackRF RFI survey sweep and register the CSV as a capture.

    Proxies the daemon's ``rfi_sweep`` control command (blocking — the live
    frame stream pauses for the sweep's duration; 409 while a capture is
    running, 503 when the daemon is unreachable), then registers the CSV as a
    :class:`Capture` (device ``hackrf``, format ``hackrf_sweep_csv``, linked
    to the running observation if any). Returns the daemon reply plus
    ``capture_id``.
    """
    params = body or RfiSweepBody()
    command = {
        "cmd": "rfi_sweep",
        "freq_lo_mhz": params.freq_lo_mhz,
        "freq_hi_mhz": params.freq_hi_mhz,
    }
    reply = await asyncio.to_thread(
        ctl_request,
        request.app.state.settings.ctl_endpoint,
        command,
        timeout_ms=_SWEEP_CTL_TIMEOUT_MS,
    )
    if not reply.get("ok"):
        error = str(reply.get("error", "unknown control error"))
        status = 503 if any(marker in error for marker in _UNREACHABLE_MARKERS) else 409
        raise HTTPException(status_code=status, detail=error)
    reply["capture_id"] = await asyncio.to_thread(
        register_rfi_sweep_capture, request.app.state.engine, reply
    )
    return reply


# ---- shared helpers ----------------------------------------------------------------


def _require_npz(capture: Capture) -> None:
    """409 unless the capture is reduced spectra (raw SigMF has no spectra route yet)."""
    if capture.format != "npz_spectra":
        raise HTTPException(
            status_code=409,
            detail=f"capture format {capture.format!r} not supported (npz_spectra only for now)",
        )


def _averaged(capture: Capture) -> tuple[np.ndarray, np.ndarray]:
    """The capture's averaged spectrum; 404 when the file is gone from disk."""
    try:
        return averaged_spectrum(capture.path)
    except FileNotFoundError:
        raise HTTPException(
            status_code=404, detail=f"capture file missing: {capture.path}"
        ) from None


def _pointing_context(
    session: Session, capture: Capture
) -> tuple[SkyCoord, Location, datetime] | None:
    """Coord + location + reference time from the capture's linked observation.

    ``None`` when the capture has no observation (or the link is broken) —
    callers fall back to the fixed window / 409 per route semantics. The
    reference time is the capture start, falling back to the observation
    start, then the row's creation time.
    """
    if capture.observation_id is None:
        return None
    observation = session.get(Observation, capture.observation_id)
    if observation is None:
        return None
    source = session.get(RadioSource, observation.source_id)
    location = session.get(Location, observation.location_id)
    if source is None or location is None:
        return None
    when = capture.start or observation.actual_start or capture.created_at
    return source_coord(source, when=when), location, when


def _plot_path(data_dir: str, capture_id: int) -> Path:
    """Where the verdict plot for a capture is rendered."""
    return Path(data_dir) / "plots" / f"capture-{capture_id}-{CLASSIFIER_NAME}.png"


def _classify(session: Session, capture: Capture, data_dir: str) -> tuple[ClassifierResult, Path]:
    """Run ``hline_v1`` on a capture: append a result row + render the plot."""
    pointing = _pointing_context(session, capture)
    try:
        if pointing is not None:
            coord, location, when = pointing
            verdict = classify_capture_npz(
                capture.path,
                lat_deg=location.lat_deg,
                lon_deg=location.lon_deg,
                elevation_m=location.elevation_m,
                coord=coord,
                when=when,
            )
        else:
            # No pointing: the fixed window is used and the location is moot.
            verdict = classify_capture_npz(capture.path, lat_deg=0.0, lon_deg=0.0, elevation_m=0.0)
    except FileNotFoundError:
        raise HTTPException(
            status_code=404, detail=f"capture file missing: {capture.path}"
        ) from None
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    assert capture.id is not None  # fetched by primary key
    result = ClassifierResult(
        capture_id=capture.id,
        name=verdict.name,
        version=verdict.version,
        verdict=verdict.verdict,
        score=verdict.score,
        params=verdict.params,
        mode="post",
    )
    session.add(result)
    session.commit()
    session.refresh(result)
    freq_hz, power_db = _averaged(capture)
    plot = verdict_plot(freq_hz, power_db, verdict, _plot_path(data_dir, capture.id))
    return result, plot


def _capture_results(session: Session, capture_id: int) -> list[ClassifierResult]:
    """A capture's classifier results, oldest first."""
    rows = session.exec(
        select(ClassifierResult)
        .where(ClassifierResult.capture_id == capture_id)
        .order_by(col(ClassifierResult.id))
    ).all()
    return list(rows)


# ---- JSON API ---------------------------------------------------------------------


@router.get("/api/captures")
def api_captures(session: SessionDep) -> list[dict[str, Any]]:
    """All captures, newest first."""
    rows = session.exec(select(Capture).order_by(col(Capture.id).desc())).all()
    return [
        {
            "id": c.id,
            "path": c.path,
            "format": c.format,
            "device": c.device,
            "size_bytes": c.size_bytes,
            "observation_id": c.observation_id,
            "start": c.start,
            "end": c.end,
            "created_at": c.created_at,
        }
        for c in rows
    ]


@router.post("/api/captures/import")
def api_captures_import(request: Request, session: SessionDep) -> dict[str, int]:
    """Register loose capture files under ``<data_dir>/captures/``.

    Scans for ``.npz`` and ``.sigmf-meta`` files with no :class:`Capture` row
    (matched by path; SigMF rows store the ``.sigmf-data`` path, same as the
    daemon registration). Imported rows carry no observation link.
    """
    captures_dir = Path(request.app.state.settings.data_dir) / "captures"
    known = {row.path for row in session.exec(select(Capture)).all()}
    imported = 0
    candidates = [(p, "npz_spectra") for p in sorted(captures_dir.glob("*.npz"))]
    candidates += [
        (meta.with_suffix(".sigmf-data"), "sigmf")
        for meta in sorted(captures_dir.glob("*.sigmf-meta"))
    ]
    for path, fmt in candidates:
        if str(path) in known:
            continue
        settings = parse_capture_settings(path)
        session.add(
            Capture(
                device=str(settings.get("source", "unknown")),
                path=str(path),
                format=fmt,
                size_bytes=path.stat().st_size if path.exists() else 0,
                sdr_settings=settings,
            )
        )
        imported += 1
    session.commit()
    return {"imported": imported}


@router.get("/api/captures/{capture_id}")
def api_capture_meta(session: SessionDep, capture_id: int) -> dict[str, Any]:
    """One capture in full: path, format, size, times, SDR settings, links
    (``purged_at`` is set once the on-disk file(s) were reclaimed)."""
    return get_or_404(session, Capture, capture_id).model_dump()


def _capture_file_paths(capture: Capture) -> list[Path]:
    """On-disk file(s) backing a capture. SigMF is a ``.sigmf-data`` +
    ``.sigmf-meta`` pair around the stored base path; every other format is the
    single stored file."""
    path = Path(capture.path)
    if capture.format == "sigmf":
        base = path.with_suffix("") if path.suffix in {".sigmf-data", ".sigmf-meta"} else path
        return [base.with_suffix(".sigmf-data"), base.with_suffix(".sigmf-meta"), path]
    return [path]


@router.post("/captures/{capture_id}/purge")
def capture_purge(session: SessionDep, capture_id: int) -> RedirectResponse:
    """Reclaim a capture's on-disk file(s) — the 43 GB/h SigMF reclaim path
    (roadmap M6) — while keeping the DB row and all provenance (settings,
    ClassifierResult rows). Idempotent; HTML-only, never an MCP verb."""
    capture = get_or_404(session, Capture, capture_id)
    for file in dict.fromkeys(_capture_file_paths(capture)):  # de-dup, keep order
        try:
            file.unlink(missing_ok=True)
        except OSError:  # a directory in the way, permissions — best-effort reclaim
            pass
    if capture.purged_at is None:
        capture.purged_at = utcnow()
        session.add(capture)
        session.commit()
    if capture.observation_id is not None:
        return RedirectResponse(f"/observations/{capture.observation_id}", status_code=303)
    return RedirectResponse("/observations", status_code=303)


@router.get("/api/captures/{capture_id}/spectrum")
def api_capture_spectrum(
    session: SessionDep, capture_id: int, axis: Literal["mhz", "vlsr"] = "mhz"
) -> dict[str, Any]:
    """The capture's averaged spectrum with a MHz or v_LSR axis.

    ``axis=vlsr`` needs a linked observation (pointing + location) — 409
    otherwise; 409 too for raw SigMF captures (spectra-only for now).
    """
    capture = get_or_404(session, Capture, capture_id)
    _require_npz(capture)
    freq_hz, power_db = _averaged(capture)
    if axis == "mhz":
        return {"axis": (freq_hz / 1e6).tolist(), "power_db": power_db.tolist(), "axis_kind": "mhz"}
    pointing = _pointing_context(session, capture)
    if pointing is None:
        raise HTTPException(
            status_code=409,
            detail="v_LSR axis needs a linked observation (pointing + location + time)",
        )
    coord, location, when = pointing
    velocity = vlsr_axis(
        freq_hz, coord, location.lat_deg, location.lon_deg, location.elevation_m, when
    )
    return {"axis": velocity.tolist(), "power_db": power_db.tolist(), "axis_kind": "vlsr"}


@router.post("/api/captures/{capture_id}/classify")
def api_capture_classify(request: Request, session: SessionDep, capture_id: int) -> dict[str, Any]:
    """Run the deterministic ``hline_v1`` classifier over a ``.npz`` capture.

    The Doppler window comes from the linked observation's pointing/location
    at the capture time when available, else the fixed fallback window. Every
    run appends a fresh :class:`ClassifierResult` row (mode ``"post"``) and
    renders the verdict plot.
    """
    capture = get_or_404(session, Capture, capture_id)
    _require_npz(capture)
    result, plot = _classify(session, capture, request.app.state.settings.data_dir)
    return {**result.model_dump(), "plot_path": str(plot)}


@router.get("/api/captures/{capture_id}/results")
def api_capture_results(session: SessionDep, capture_id: int) -> list[dict[str, Any]]:
    """The capture's classifier result rows, oldest first."""
    get_or_404(session, Capture, capture_id)
    return [r.model_dump() for r in _capture_results(session, capture_id)]


@router.get("/api/captures/{capture_id}/plot")
def api_capture_plot(request: Request, session: SessionDep, capture_id: int) -> FileResponse:
    """The rendered verdict plot PNG; 404 until a classify run renders one."""
    get_or_404(session, Capture, capture_id)
    path = _plot_path(request.app.state.settings.data_dir, capture_id)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="no verdict plot rendered for this capture yet")
    return FileResponse(path, media_type="image/png")


# ---- htmx fragment (observation detail page) ---------------------------------------


@router.post("/captures/{capture_id}/classify", response_class=HTMLResponse)
def capture_classify_fragment(
    request: Request, session: SessionDep, capture_id: int
) -> HTMLResponse:
    """Classify (htmx) and re-render the capture's confirmation cell."""
    capture = get_or_404(session, Capture, capture_id)
    _require_npz(capture)
    _classify(session, capture, request.app.state.settings.data_dir)
    return TEMPLATES.TemplateResponse(
        request,
        "_capture_results.html",
        {"capture": capture, "results": _capture_results(session, capture_id)},
    )
