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
from typing import Annotated, Any, Literal

import numpy as np
from astropy.coordinates import SkyCoord
from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy import Engine
from sqlmodel import Session, col, select

from jansky_observe.astro.hi_reference import reference_profile
from jansky_observe.astro.lsr import vlsr_axis
from jansky_observe.astro.pointing import sidereal_day_number
from jansky_observe.confirm.classifier import (
    CLASSIFIER_NAME,
    CLASSIFIER_ONOFF_NAME,
    averaged_spectrum,
    classify_capture_npz,
    classify_difference_npz,
)
from jansky_observe.confirm.noise import power_distribution
from jansky_observe.confirm.onoff import difference_spectrum
from jansky_observe.confirm.plots import verdict_plot
from jansky_observe.confirm.radiometer import radiometer_estimate
from jansky_observe.control import ctl_request
from jansky_observe.export.figures import profile_overlay_figure, total_power_histogram_figure
from jansky_observe.models import (
    CAPTURE_POSITIONS,
    CalibrationEpoch,
    Campaign,
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


def latest_cal_epoch_id(session: Session) -> int | None:
    """The most recent calibration epoch's id, or ``None`` (roadmap M7).

    A science capture is stamped with this at registration — the calibration in
    effect when it was taken (plan 79's cal-epoch provenance)."""
    epoch = session.exec(
        select(CalibrationEpoch).order_by(col(CalibrationEpoch.started_at).desc())
    ).first()
    return None if epoch is None else epoch.id


def active_campaign(session: Session) -> Campaign | None:
    """The active drift-scan campaign, or ``None`` (roadmap M7). New captures are
    tagged into it (campaign_id + sidereal_day) at registration."""
    return session.exec(
        select(Campaign).where(Campaign.status == "active").order_by(col(Campaign.id).desc())
    ).first()


def _default_lon_deg(session: Session) -> float:
    location = session.exec(
        select(Location).where(Location.is_default == True)  # noqa: E712
    ).first()
    return 0.0 if location is None else location.lon_deg


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
        campaign = active_campaign(session)
        capture = Capture(
            observation_id=_running_observation_id(session),
            device=str(status.get("source", "unknown")),
            path=str(path),
            format=fmt,
            size_bytes=int(status.get("bytes_written") or 0),
            start=started,
            end=ended,
            sdr_settings=parse_capture_settings(Path(str(path))),
            cal_epoch_id=latest_cal_epoch_id(session),  # science capture: cal in effect
            campaign_id=None if campaign is None else campaign.id,
            sidereal_day=(
                None
                if campaign is None
                else sidereal_day_number(_default_lon_deg(session), started)
            ),
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


# ---- ON/OFF position switching + difference (roadmap M10) --------------------------


def _difference_plot_path(data_dir: str, capture_id: int) -> Path:
    """Where the ON−OFF difference verdict plot for an ON capture is rendered
    (distinct from the single-capture :func:`_plot_path`)."""
    return Path(data_dir) / "plots" / f"capture-{capture_id}-{CLASSIFIER_ONOFF_NAME}.png"


def _paired_off_id(session: Session, on_capture: Capture) -> int | None:
    """The id of the OFF capture paired to this ON (``pair_capture_id == on.id``),
    or ``None`` — drives the "Classify difference" button in the detail page."""
    if on_capture.id is None:
        return None
    off = session.exec(
        select(Capture)
        .where(Capture.pair_capture_id == on_capture.id)
        .where(Capture.position == "off")
        .order_by(col(Capture.id))
    ).first()
    return None if off is None else off.id


def _resolve_off(session: Session, on_capture: Capture, ref: int | None) -> Capture:
    """The OFF capture to difference against: ``ref`` when given, else inferred
    from the OFF whose ``pair_capture_id`` points at this ON.

    Validates the OFF exists, is an ``"off"`` position, and shares the ON's
    observation — 422 otherwise (a bad or mismatched reference)."""
    if ref is None:
        off = session.exec(
            select(Capture)
            .where(Capture.pair_capture_id == on_capture.id)
            .where(Capture.position == "off")
            .order_by(col(Capture.id))
        ).first()
        if off is None:
            raise HTTPException(
                status_code=422,
                detail="no paired OFF capture — pass ?ref=<off_id> or pair one on the detail page",
            )
    else:
        off = session.get(Capture, ref)
        if off is None:
            raise HTTPException(status_code=422, detail=f"reference capture {ref} not found")
    if off.position != "off":
        raise HTTPException(
            status_code=422, detail=f"reference capture {off.id} is not an OFF position"
        )
    if off.observation_id != on_capture.observation_id:
        raise HTTPException(
            status_code=422,
            detail="ON and OFF captures belong to different observations",
        )
    return off


def _difference(
    session: Session, on_capture: Capture, off_capture: Capture, method: str
) -> tuple[np.ndarray, np.ndarray]:
    """The ON−OFF difference spectrum; 404 if a file is missing, 422 on mismatch."""
    on_freq, on_db = _averaged(on_capture)
    off_freq, off_db = _averaged(off_capture)
    try:
        return difference_spectrum(on_freq, on_db, off_freq, off_db, method=method)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


def _classify_difference(
    session: Session, on_capture: Capture, off_capture: Capture, method: str, data_dir: str
) -> tuple[ClassifierResult, Path]:
    """Classify the ON−OFF difference: append a ``hline_v1_onoff`` result row
    (with ``ref_capture_id`` + ``method`` in params) and render its plot."""
    pointing = _pointing_context(session, on_capture)
    try:
        if pointing is not None:
            coord, location, when = pointing
            verdict = classify_difference_npz(
                on_capture.path,
                off_capture.path,
                lat_deg=location.lat_deg,
                lon_deg=location.lon_deg,
                elevation_m=location.elevation_m,
                coord=coord,
                when=when,
                method=method,
            )
        else:
            verdict = classify_difference_npz(
                on_capture.path,
                off_capture.path,
                lat_deg=0.0,
                lon_deg=0.0,
                elevation_m=0.0,
                method=method,
            )
    except FileNotFoundError:
        raise HTTPException(
            status_code=404, detail=f"capture file missing: {on_capture.path} / {off_capture.path}"
        ) from None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    assert on_capture.id is not None  # fetched by primary key
    result = ClassifierResult(
        capture_id=on_capture.id,
        name=verdict.name,
        version=verdict.version,
        verdict=verdict.verdict,
        score=verdict.score,
        params={**verdict.params, "ref_capture_id": off_capture.id},
        mode="post",
    )
    session.add(result)
    session.commit()
    session.refresh(result)
    freq_hz, diff_db = _difference(session, on_capture, off_capture, method)
    plot = verdict_plot(freq_hz, diff_db, verdict, _difference_plot_path(data_dir, on_capture.id))
    return result, plot


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


@router.post("/captures/{capture_id}/position")
def set_capture_position(
    session: SessionDep,
    capture_id: int,
    position: Annotated[str, Form()],
    pair_capture_id: Annotated[str, Form()] = "",
) -> RedirectResponse:
    """Set a capture's ON/OFF position role (roadmap M10). HTML-only.

    ``position`` is ``"on"`` or ``"off"`` (422 otherwise). Setting ``"off"``
    optionally pairs it with an ON capture via the submitted ``pair_capture_id``
    — validated to be an ``"on"`` capture in the *same* observation (422
    otherwise); an empty field clears the pairing. Setting ``"on"`` always clears
    the pairing. Redirects back to the observation."""
    if position not in CAPTURE_POSITIONS:
        raise HTTPException(status_code=422, detail=f"unknown capture position {position!r}")
    capture = get_or_404(session, Capture, capture_id)
    if position == "off":
        pair_id = pair_capture_id.strip()
        if pair_id:
            pair = session.get(Capture, int(pair_id))
            if pair is None or pair.position != "on":
                raise HTTPException(
                    status_code=422, detail=f"pair capture {pair_id} is not an ON capture"
                )
            if pair.observation_id != capture.observation_id:
                raise HTTPException(
                    status_code=422, detail="pair capture is in a different observation"
                )
            capture.pair_capture_id = pair.id
        else:
            capture.pair_capture_id = None
    else:  # on: an ON capture never references a pair
        capture.pair_capture_id = None
    capture.position = position
    session.add(capture)
    session.commit()
    dest = f"/observations/{capture.observation_id}" if capture.observation_id else "/observations"
    return RedirectResponse(dest, status_code=303)


def _dsp_params(capture: Capture) -> tuple[float, float]:
    """``(channel_bw_hz, integration_s)`` read from a capture's ``.npz``.

    Channel bandwidth is ``sample_rate_hz / n_fft``; integration time is the span
    of the recorded frame timestamps, falling back to the row's start/end span.
    Raises ``FileNotFoundError`` when the file is gone (caller maps to 404).
    """
    with np.load(Path(capture.path), allow_pickle=False) as data:
        power_db = np.asarray(data["power_db"], dtype=np.float64)
        sample_rate_hz = float(data["sample_rate_hz"])
        timestamps = np.asarray(data.get("timestamps", []), dtype=np.float64)
    n_fft = power_db.shape[1] if power_db.ndim == 2 else power_db.shape[0]
    channel_bw_hz = sample_rate_hz / n_fft
    integration_s = float(timestamps[-1] - timestamps[0]) if timestamps.size > 1 else 0.0
    if integration_s <= 0.0 and capture.start is not None and capture.end is not None:
        integration_s = max((capture.end - capture.start).total_seconds(), 0.0)
    return channel_bw_hz, integration_s


def _radiometer_for_capture(session: Session, capture: Capture) -> dict[str, Any]:
    """The radiometer estimate for a capture, or ``{"available": False, ...}``.

    Needs an M10 sky/ground Tsys on the capture's calibration epoch; degrades with
    a reason (no epoch / no Tsys / no integration time) rather than raising. An
    estimate, never a verdict (plan §12.5)."""
    if capture.cal_epoch_id is None:
        return {"available": False, "reason": "capture has no calibration epoch"}
    epoch = session.get(CalibrationEpoch, capture.cal_epoch_id)
    if epoch is None or epoch.tsys_k is None:
        return {"available": False, "reason": "no Tsys on the calibration epoch (run sky/ground)"}
    try:
        channel_bw_hz, integration_s = _dsp_params(capture)
    except FileNotFoundError:
        return {"available": False, "reason": "capture file missing"}
    if integration_s <= 0.0:
        return {"available": False, "reason": "no integration time recorded"}
    estimate = radiometer_estimate(
        tsys_k=epoch.tsys_k, channel_bw_hz=channel_bw_hz, integration_s=integration_s
    )
    return {"available": True, **estimate}


@router.get("/api/captures/{capture_id}/radiometer")
def api_capture_radiometer(session: SessionDep, capture_id: int) -> dict[str, Any]:
    """The radiometer-equation estimate for a capture (roadmap M12): the theoretical
    noise floor ΔT_rms, the predicted SNR of an assumed HI line, and the integration
    time to reach SNR 5 — from the capture's Tsys (M10 sky/ground cal), per-channel
    bandwidth, and integration time. An advisory ESTIMATE that rides alongside the
    classifier's empirical SNR — never a detection verdict. Returns
    ``{"available": false, "reason": ...}`` when Tsys/integration is missing."""
    capture = get_or_404(session, Capture, capture_id)
    _require_npz(capture)
    return _radiometer_for_capture(session, capture)


def _load_power_db(capture: Capture) -> np.ndarray:
    """The capture's ``(n_frames, n_fft)`` power_db array; 404 if the file is gone."""
    try:
        with np.load(Path(capture.path), allow_pickle=False) as data:
            return np.asarray(data["power_db"], dtype=np.float64)
    except FileNotFoundError:
        raise HTTPException(
            status_code=404, detail=f"capture file missing: {capture.path}"
        ) from None


@router.get("/api/captures/{capture_id}/noise")
def api_capture_noise(session: SessionDep, capture_id: int) -> dict[str, Any]:
    """The capture's total-power noise diagnostic (roadmap M12): the per-frame
    power distribution's Gaussian fit (mean/sigma), skew, excess kurtosis, and a
    non_gaussian flag — a departure from Gaussian is an RFI/saturation tell. A
    diagnostic, not a verdict. 422 for a single-frame capture (needs ≥3 frames)."""
    capture = get_or_404(session, Capture, capture_id)
    _require_npz(capture)
    try:
        return power_distribution(_load_power_db(capture)).stats()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


@router.get("/api/captures/{capture_id}/power_histogram.png")
def api_capture_power_histogram(
    request: Request, session: SessionDep, capture_id: int
) -> FileResponse:
    """The total-power histogram + Gaussian fit PNG (roadmap M12)."""
    capture = get_or_404(session, Capture, capture_id)
    _require_npz(capture)
    try:
        dist = power_distribution(_load_power_db(capture))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    out = Path(request.app.state.settings.data_dir) / "plots" / f"capture-{capture_id}-noise.png"
    total_power_histogram_figure(
        dist, out, title=f"Capture {capture_id} — total-power distribution"
    )
    return FileResponse(out, media_type="image/png")


def _overlay_for_capture(session: Session, capture: Capture, data_dir: str) -> dict[str, Any]:
    """Observed spectrum on a v_LSR axis + the reference HI model overlay (roadmap M12).

    ``{"available": False, "reason": ...}`` when there is no pointing (can't know the
    galactic direction) or no model was obtained (offline / off-survey). The model is
    a visual aid, never a verdict."""
    pointing = _pointing_context(session, capture)
    if pointing is None:
        return {"available": False, "reason": "needs a linked observation (pointing + time)"}
    coord, location, when = pointing
    freq_hz, power_db = _averaged(capture)
    velocity = vlsr_axis(
        freq_hz, coord, location.lat_deg, location.lon_deg, location.elevation_m, when
    )
    gal = coord.galactic
    l_deg, b_deg = float(gal.l.deg), float(gal.b.deg)
    observed = {"v_lsr_kms": velocity.tolist(), "power_db": power_db.tolist()}
    model = reference_profile(
        l_deg, b_deg, provider="web", cache_dir=str(Path(data_dir) / "hi_reference")
    )
    if model is None:
        return {
            "available": False,
            "reason": "no reference model for this direction (offline or off-survey)",
            "l_deg": l_deg,
            "b_deg": b_deg,
            "observed": observed,
        }
    return {
        "available": True,
        "l_deg": l_deg,
        "b_deg": b_deg,
        "observed": observed,
        "model": {
            "source": model.source,
            "v_lsr_kms": model.v_lsr_kms.tolist(),
            "t_b_k": model.t_b_k.tolist(),
            "peak_t_b_k": model.peak_t_b_k,
        },
    }


@router.get("/api/captures/{capture_id}/overlay")
def api_capture_overlay(request: Request, session: SessionDep, capture_id: int) -> dict[str, Any]:
    """The observed spectrum + a reference HI-survey model overlay (roadmap M12).

    Returns the observed spectrum on a v_LSR axis and the expected LAB profile for
    the capture's galactic direction (a shape comparison — observed is relative
    power). ``{"available": false, "reason": ...}`` when there's no pointing or no
    model (offline / off-survey). A visual aid, NOT a detection verdict."""
    capture = get_or_404(session, Capture, capture_id)
    _require_npz(capture)
    return _overlay_for_capture(session, capture, request.app.state.settings.data_dir)


@router.get("/api/captures/{capture_id}/overlay.png")
def api_capture_overlay_png(request: Request, session: SessionDep, capture_id: int) -> FileResponse:
    """The observed-vs-reference-model overlay PNG (roadmap M12). 409 when no model
    is available (offline / off-survey / no pointing)."""
    capture = get_or_404(session, Capture, capture_id)
    _require_npz(capture)
    overlay = _overlay_for_capture(session, capture, request.app.state.settings.data_dir)
    if not overlay["available"]:
        raise HTTPException(status_code=409, detail=overlay["reason"])
    observed, model = overlay["observed"], overlay["model"]
    out = Path(request.app.state.settings.data_dir) / "plots" / f"capture-{capture_id}-overlay.png"
    profile_overlay_figure(
        np.asarray(observed["v_lsr_kms"]),
        np.asarray(observed["power_db"]),
        np.asarray(model["v_lsr_kms"]),
        np.asarray(model["t_b_k"]),
        out,
        title=f"Capture {capture_id} — observed vs {model['source']} model",
        model_source=model["source"],
    )
    return FileResponse(out, media_type="image/png")


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


@router.get("/api/captures/{capture_id}/difference")
def api_capture_difference(
    session: SessionDep,
    capture_id: int,
    ref: int | None = None,
    axis: Literal["mhz", "vlsr"] = "mhz",
    method: Literal["ratio", "subtract"] = "ratio",
) -> dict[str, Any]:
    """The ON−OFF difference spectrum of an ON capture, same shape as
    ``/spectrum`` (roadmap M10).

    ``ref`` is the OFF capture id; omit it to infer the OFF paired to this ON.
    404 if either file is purged/missing; 422 on an axis mismatch or a ``ref``
    that is not a valid OFF for this observation; ``axis=vlsr`` needs a linked
    observation (409 otherwise, like ``/spectrum``).
    """
    on_capture = get_or_404(session, Capture, capture_id)
    _require_npz(on_capture)
    off_capture = _resolve_off(session, on_capture, ref)
    _require_npz(off_capture)
    freq_hz, diff_db = _difference(session, on_capture, off_capture, method)
    if axis == "mhz":
        return {
            "axis": (freq_hz / 1e6).tolist(),
            "power_db": diff_db.tolist(),
            "axis_kind": "mhz",
            "ref_capture_id": off_capture.id,
        }
    pointing = _pointing_context(session, on_capture)
    if pointing is None:
        raise HTTPException(
            status_code=409,
            detail="v_LSR axis needs a linked observation (pointing + location + time)",
        )
    coord, location, when = pointing
    velocity = vlsr_axis(
        freq_hz, coord, location.lat_deg, location.lon_deg, location.elevation_m, when
    )
    return {
        "axis": velocity.tolist(),
        "power_db": diff_db.tolist(),
        "axis_kind": "vlsr",
        "ref_capture_id": off_capture.id,
    }


@router.post("/api/captures/{capture_id}/classify_difference")
def api_capture_classify_difference(
    request: Request,
    session: SessionDep,
    capture_id: int,
    ref: int | None = None,
    method: Literal["ratio", "subtract"] = "ratio",
) -> dict[str, Any]:
    """Classify the ON−OFF difference of an ON capture (roadmap M10).

    Resolves the OFF from ``ref`` (or the pairing), runs the difference +
    ``hline_v1_onoff`` classify, appends a :class:`ClassifierResult` (params
    carry ``ref_capture_id`` + ``method`` alongside the peak/SNR fields), and
    renders the difference verdict plot. 422 on a bad ``ref`` or axis mismatch;
    404 if a file is missing.
    """
    on_capture = get_or_404(session, Capture, capture_id)
    _require_npz(on_capture)
    off_capture = _resolve_off(session, on_capture, ref)
    _require_npz(off_capture)
    result, plot = _classify_difference(
        session, on_capture, off_capture, method, request.app.state.settings.data_dir
    )
    return {**result.model_dump(), "plot_path": str(plot)}


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


@router.get("/api/captures/{capture_id}/difference_plot")
def api_capture_difference_plot(
    request: Request, session: SessionDep, capture_id: int
) -> FileResponse:
    """The rendered ON−OFF difference verdict plot PNG; 404 until a
    classify-difference run renders one."""
    get_or_404(session, Capture, capture_id)
    path = _difference_plot_path(request.app.state.settings.data_dir, capture_id)
    if not path.is_file():
        raise HTTPException(
            status_code=404, detail="no difference verdict plot rendered for this capture yet"
        )
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
        {
            "capture": capture,
            "results": _capture_results(session, capture_id),
            "pair_off_id": _paired_off_id(session, capture),
        },
    )


@router.post("/captures/{capture_id}/classify_difference", response_class=HTMLResponse)
def capture_classify_difference_fragment(
    request: Request,
    session: SessionDep,
    capture_id: int,
    ref: Annotated[int | None, Form()] = None,
    method: Annotated[str, Form()] = "ratio",
) -> HTMLResponse:
    """Classify the ON−OFF difference (htmx) and re-render the ON capture's
    confirmation cell, with the ``hline_v1_onoff`` verdict alongside any
    single-capture ones."""
    on_capture = get_or_404(session, Capture, capture_id)
    _require_npz(on_capture)
    off_capture = _resolve_off(session, on_capture, ref)
    _require_npz(off_capture)
    _classify_difference(
        session, on_capture, off_capture, method, request.app.state.settings.data_dir
    )
    return TEMPLATES.TemplateResponse(
        request,
        "_capture_results.html",
        {
            "capture": on_capture,
            "results": _capture_results(session, capture_id),
            "pair_off_id": off_capture.id,
        },
    )
