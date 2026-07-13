"""Unattended capture scheduler (roadmap M7, plans 79/84).

A schedule fires a capture that starts ``lead_min`` before its source's transit
and runs ``run_min``; ``repeat`` is ``once`` or ``daily``. The server runs the
loop, but the **capture daemon stays the only SDR owner** — the scheduler drives
it through the same control channel the UI uses, and never starts a capture while
one is already running. A run that would push the data disk past the red
threshold is refused and the reason recorded.

Design: the firing decision is a pure function (:func:`next_decision`) over
already-computed windows, so it unit-tests without astropy, a daemon, or a
clock. :func:`scheduler_tick` wraps it with the IO (transit computation, the
disk guardrail, the control channel); :func:`scheduler_loop` is the lifespan
task that ticks it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlmodel import Session, col, select

from jansky_observe.astro.pointing import transit_info
from jansky_observe.control import ctl_request
from jansky_observe.models import Location, RadioSource, Schedule, utcnow
from jansky_observe.server.live_badge import source_coord

if TYPE_CHECKING:  # pragma: no cover
    from fastapi import FastAPI

logger = logging.getLogger(__name__)

__all__ = [
    "SchedulerState",
    "firing_window",
    "next_decision",
    "projected_capture_bytes",
    "scheduler_loop",
    "scheduler_tick",
    "would_exceed_disk_red",
]

SCHED_TICK_S = 20.0
CTL_TIMEOUT_MS = 1500
_DISK_RED_FRACTION = 0.10
_SIGMF_BYTES_PER_SAMPLE = 4  # ci16_le
_SECONDS_PER_MIN = 60.0


@dataclass
class SchedulerState:
    """What the scheduler is doing right now (lives on ``app.state``)."""

    running_schedule_id: int | None = None
    stop_at: datetime | None = None
    note: str = ""
    #: schedule id → the human-readable reason it last did not fire / did fire.
    notes: dict[int, str] = field(default_factory=dict)


def firing_window(lead_min: float, run_min: float, transit: datetime) -> tuple[datetime, datetime]:
    """The capture window for a schedule: ``[transit - lead, that + run]``."""
    start = transit - timedelta(minutes=lead_min)
    return start, start + timedelta(minutes=run_min)


def projected_capture_bytes(
    fmt: str, run_min: float, sample_rate_hz: float, n_fft: int = 2048, fps: float = 4.0
) -> float:
    """Bytes a ``run_min`` capture would write. SigMF is raw IQ
    (``sample_rate × 4``); ``.npz`` is reduced spectra (``n_fft × 4 × fps``)."""
    seconds = run_min * _SECONDS_PER_MIN
    if fmt == "sigmf":
        return sample_rate_hz * _SIGMF_BYTES_PER_SAMPLE * seconds
    return n_fft * 4.0 * fps * seconds


def would_exceed_disk_red(free_bytes: float, total_bytes: float, projected_bytes: float) -> bool:
    """True if writing ``projected_bytes`` would drop free space below the red
    threshold (10 %) — the unattended-run guardrail."""
    if total_bytes <= 0:
        return False
    return (free_bytes - projected_bytes) / total_bytes < _DISK_RED_FRACTION


def next_decision(
    windows: list[tuple[Schedule, datetime, datetime]],
    now: datetime,
    state: SchedulerState,
    capturing: bool,
) -> tuple[str, Schedule | None]:
    """Decide the next scheduler action from the computed windows (pure).

    Returns ``("stop", schedule)`` when the running scheduled capture is past its
    stop time, ``("start", schedule)`` for the first schedule whose window is
    open now (and hasn't fired for it) — only when nothing is capturing — or
    ``("none", None)``.
    """
    if state.running_schedule_id is not None:
        running = next((s for s, _, _ in windows if s.id == state.running_schedule_id), None)
        if state.stop_at is not None and now >= state.stop_at:
            return "stop", running
        return "none", None
    if capturing:
        return "none", None  # a manual (or other) capture owns the SDR — don't interfere
    for schedule, start, stop in sorted(windows, key=lambda w: w[1]):
        if start <= now < stop and (schedule.last_run_at is None or schedule.last_run_at < start):
            return "start", schedule
    return "none", None


def _default_location(session: Session) -> Location | None:
    return session.exec(
        select(Location).where(Location.is_default == True)  # noqa: E712
    ).first()


def _compute_windows(session: Session, now: datetime) -> list[tuple[Schedule, datetime, datetime]]:
    """Firing window per enabled schedule, from each source's next transit."""
    location = _default_location(session)
    if location is None:
        return []
    import astropy.units as u
    from astropy.coordinates import EarthLocation

    earth = EarthLocation(
        lat=location.lat_deg * u.deg,
        lon=location.lon_deg * u.deg,
        height=location.elevation_m * u.m,
    )
    windows: list[tuple[Schedule, datetime, datetime]] = []
    schedules = session.exec(select(Schedule).where(col(Schedule.enabled) == True)).all()  # noqa: E712
    for schedule in schedules:
        source = session.get(RadioSource, schedule.source_id)
        if source is None:
            continue
        coord = source_coord(source, now)
        transit = transit_info(coord, earth, when=now).time_utc
        windows.append((schedule, *firing_window(schedule.lead_min, schedule.run_min, transit)))
    return windows


async def scheduler_tick(app: FastAPI, now: datetime | None = None) -> None:
    """Evaluate the schedules once and drive the daemon accordingly."""
    settings = app.state.settings
    engine = app.state.engine
    state: SchedulerState = app.state.scheduler
    if engine is None:
        return
    now = utcnow() if now is None else now

    def _ctl(request: dict[str, Any]) -> dict[str, Any]:
        return ctl_request(settings.ctl_endpoint, request, timeout_ms=CTL_TIMEOUT_MS)

    status = _ctl({"cmd": "status"})
    capturing = bool(status.get("ok")) and bool(status.get("capturing"))

    with Session(engine) as session:
        windows = await asyncio.to_thread(_compute_windows, session, now)
        action, schedule = next_decision(windows, now, state, capturing)

        if action == "stop" and state.running_schedule_id is not None:
            reply = _ctl({"cmd": "stop_capture"})
            from jansky_observe.server.routers.captures import register_stopped_capture

            await asyncio.to_thread(register_stopped_capture, engine, reply)
            if schedule is not None and schedule.repeat == "once":
                schedule.enabled = False
                session.add(schedule)
                session.commit()
            state.note = f"stopped schedule {state.running_schedule_id}"
            state.running_schedule_id = None
            state.stop_at = None
            return

        if action == "start" and schedule is not None:
            window = next((w for w in windows if w[0].id == schedule.id), None)
            start, stop = (window[1], window[2]) if window else (now, now)
            sample_rate = _sample_rate(app)
            projected = projected_capture_bytes(schedule.format, schedule.run_min, sample_rate)
            usage = shutil.disk_usage(settings.data_dir)
            schedule.last_run_at = start  # consume this window either way
            if would_exceed_disk_red(usage.free, usage.total, projected):
                state.notes[schedule.id or 0] = "refused: would fill the disk past red"
                session.add(schedule)
                session.commit()
                logger.warning("scheduler refused schedule %s: disk", schedule.id)
                return
            reply = _ctl({"cmd": "start_capture", "format": schedule.format})
            session.add(schedule)
            session.commit()
            if reply.get("ok"):
                state.running_schedule_id = schedule.id
                state.stop_at = stop
                state.note = f"running schedule {schedule.id} until {stop.isoformat()}"
                state.notes[schedule.id or 0] = "running"
                # Auto-slew the rotator to the schedule's source at window open
                # (roadmap M9). Best-effort: a rotator problem never fails the capture.
                await asyncio.to_thread(_auto_slew_to_source, engine, schedule.source_id, now)
            else:
                state.notes[schedule.id or 0] = f"start refused by daemon: {reply.get('error')}"


def _auto_slew_to_source(engine: Any, source_id: int, when: datetime) -> None:
    """Best-effort scheduler auto-slew: point the rotator at a source's current
    az/el. A missing rotator, limits, or transport error is logged, never raised —
    the scheduled capture proceeds regardless."""
    from jansky_observe.server.rotator import slew
    from jansky_observe.server.routers import default_location, default_station, source_pointing

    try:
        with Session(engine) as session:
            source = session.get(RadioSource, source_id)
            if source is None:
                return
            station = default_station(session)
            if station.rotator_kind == "none":
                return
            pointing = source_pointing(
                source, default_location(session), station, when=when, full=False
            )
            slew(station, float(pointing["az_deg"]), float(pointing["el_deg"]))
    except Exception:  # noqa: BLE001 - auto-slew is best-effort; never fail the capture
        logger.warning("scheduler auto-slew to source %s failed", source_id, exc_info=True)


def _sample_rate(app: FastAPI) -> float:
    latest = app.state.broadcaster.latest
    return float(latest.sample_rate_hz) if latest is not None else 3_000_000.0


async def scheduler_loop(app: FastAPI) -> None:
    """Lifespan task: tick the scheduler forever (best-effort; never crashes the
    server)."""
    logger.info("scheduler loop started (tick %.0fs)", SCHED_TICK_S)
    while True:
        await asyncio.sleep(SCHED_TICK_S)
        with contextlib.suppress(Exception):
            await scheduler_tick(app)
