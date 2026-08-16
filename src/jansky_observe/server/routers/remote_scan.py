"""Remote RFI scanner ingest + presence (plans/remote-scanner.md).

A second host (the operator's laptop, HackRF attached) runs
``deploy/remote_scan.py`` and pushes ``hackrf_sweep`` CSVs here over HTTP.
That decouples RFI surveying from the capture daemon entirely: the local
``POST /api/rfi_sweep`` pauses the live HI frame stream for the sweep's
duration, while a remote sweep never touches the daemon, so the dish
integrates continuously.

Same capture convention as the local path: the raw CSV *is* the capture,
validated by :func:`~jansky_observe.capture.hackrf_sweep.averaged_bins` and
registered as a :class:`~jansky_observe.models.Capture` (device
``hackrf-remote``, format ``hackrf_sweep_csv``) so remote sweeps appear in
the existing RFI view. Remote metadata rides in ``sdr_settings`` — no schema
change.

Presence is an in-memory last-seen registry on ``app.state`` (deliberately
ephemeral: a server restart forgets scanners, and the next heartbeat —
every 15 s from the client — re-registers). A scanner is *connected* when
its last heartbeat or upload is less than :data:`CONNECTED_MAX_AGE_S` old;
the status bar renders that as the ``scanner`` chip.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import Engine
from sqlmodel import Session

from jansky_observe.capture.hackrf_sweep import averaged_bins
from jansky_observe.models import Capture, utcnow

__all__ = ["CONNECTED_MAX_AGE_S", "router", "scanner_status"]

router = APIRouter(tags=["remote-scan"])

CONNECTED_MAX_AGE_S = 90.0
"""A scanner whose last contact is older than this is shown disconnected —
six missed 15-s heartbeats, forgiving of a sweep blocking the client loop."""

_MAX_CSV_BYTES = 50 * 1024 * 1024
"""Reject uploads past this size: a full 1 MHz-binned 1–6000 MHz sweep at 20
sweeps is a few MB; 50 MB means something is wrong on the client."""


def _registry(request: Request) -> dict[str, dict[str, Any]]:
    """The per-app scanner registry, created on first use."""
    state = request.app.state
    if not hasattr(state, "remote_scanners"):
        state.remote_scanners = {}
    registry: dict[str, dict[str, Any]] = state.remote_scanners
    return registry


def _touch(
    registry: dict[str, dict[str, Any]], host: str, note: str | None, *, uploaded: bool = False
) -> None:
    entry = registry.setdefault(host, {"n_uploads": 0, "note": None})
    entry["last_seen_unix"] = time.time()
    if note is not None:
        entry["note"] = note
    if uploaded:
        entry["n_uploads"] = int(entry.get("n_uploads", 0)) + 1
        entry["last_upload_unix"] = entry["last_seen_unix"]


def scanner_status(registry: dict[str, dict[str, Any]] | None) -> list[dict[str, Any]]:
    """The registry reduced for display: one row per scanner, newest first.

    Shared with the status bar (which imports this rather than reaching into
    ``app.state`` shapes). ``None`` (no registry yet) is an empty list.
    """
    rows: list[dict[str, Any]] = []
    now = time.time()
    for host, entry in (registry or {}).items():
        last_seen = entry.get("last_seen_unix") or 0.0
        age = now - float(last_seen)
        rows.append(
            {
                "host": host,
                "age_s": round(age, 1),
                "connected": age < CONNECTED_MAX_AGE_S,
                "n_uploads": int(entry.get("n_uploads", 0)),
                "note": entry.get("note"),
            }
        )
    rows.sort(key=lambda r: float(r["age_s"]))
    return rows


class HeartbeatBody(BaseModel):
    """``POST /api/remote_scan/heartbeat`` body."""

    host: str
    note: str | None = None


@router.post("/api/remote_scan/heartbeat")
async def api_heartbeat(request: Request, body: HeartbeatBody) -> dict[str, Any]:
    """Record that a remote scanner is alive; returns its status row."""
    registry = _registry(request)
    _touch(registry, body.host, body.note)
    return {"ok": True, "scanners": scanner_status(registry)}


@router.get("/api/remote_scan/status")
async def api_status(request: Request) -> dict[str, Any]:
    """All known scanners with connection state (in-memory; empty after restart)."""
    return {"scanners": scanner_status(_registry(request))}


def _register_remote_sweep(
    engine: Engine | None,
    path: Path,
    *,
    n_rows: int,
    freq_range_hz: tuple[float, float],
    host: str,
    antenna: str | None,
    label: str | None,
    num_sweeps: int | None,
) -> int | None:
    """The remote twin of ``captures.register_rfi_sweep_capture`` — one
    :class:`Capture` row, remote provenance in ``sdr_settings``."""
    if engine is None:
        return None
    with Session(engine) as session:
        capture = Capture(
            observation_id=None,
            device="hackrf-remote",
            path=str(path),
            format="hackrf_sweep_csv",
            size_bytes=path.stat().st_size,
            end=utcnow(),
            sdr_settings={
                "freq_range_hz": list(freq_range_hz),
                "num_sweeps": num_sweeps,
                "n_rows": n_rows,
                "remote": {"host": host, "antenna": antenna, "label": label},
            },
        )
        session.add(capture)
        session.commit()
        session.refresh(capture)
        return capture.id


@router.post("/api/remote_scan/sweep")
async def api_upload_sweep(
    request: Request,
    host: Annotated[str, Query(min_length=1, max_length=100)],
    antenna: Annotated[str | None, Query(max_length=200)] = None,
    label: Annotated[str | None, Query(max_length=200)] = None,
    num_sweeps: Annotated[int | None, Query(ge=1)] = None,
) -> dict[str, Any]:
    """Ingest a ``hackrf_sweep`` CSV pushed by a remote scanner.

    The request body is the raw CSV exactly as ``hackrf_sweep`` printed it.
    It is validated (422 when no usable rows parse), persisted under the
    same ``captures/rfi-*.csv`` convention as local sweeps with a
    ``-remote`` suffix, and registered as a :class:`Capture`. The upload
    also counts as a heartbeat for ``host``.
    """
    raw = await request.body()
    if not raw.strip():
        raise HTTPException(status_code=422, detail="empty body; expected hackrf_sweep CSV")
    if len(raw) > _MAX_CSV_BYTES:
        raise HTTPException(status_code=413, detail="CSV too large")

    data_dir = Path(request.app.state.settings.data_dir)
    captures_dir = data_dir / "captures"
    captures_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%S")
    path = captures_dir / f"rfi-{stamp}Z-remote.csv"
    path.write_bytes(raw)

    try:
        _bins, freq_range, n_rows = averaged_bins(path)
    except ValueError as exc:
        path.unlink(missing_ok=True)  # an unparseable upload is not a capture
        raise HTTPException(status_code=422, detail=f"not a hackrf_sweep CSV: {exc}") from exc

    capture_id = _register_remote_sweep(
        request.app.state.engine,
        path,
        n_rows=n_rows,
        freq_range_hz=freq_range,
        host=host,
        antenna=antenna,
        label=label,
        num_sweeps=num_sweeps,
    )
    registry = _registry(request)
    _touch(registry, host, None, uploaded=True)
    return {
        "ok": True,
        "capture_id": capture_id,
        "path": str(path),
        "n_rows": n_rows,
        "freq_range_hz": list(freq_range),
    }
