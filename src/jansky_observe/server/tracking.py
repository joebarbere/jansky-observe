"""Drift tracking: keep the dish on a moving source while observing (M9 piece 3).

While an observation is running and tracking is enabled, a best-effort loop
re-points the rotator whenever the source has drifted more than a fraction of the
beam (HPBW) from the last commanded position — a beam-crossing-aware cadence
rather than a fixed clock. Enabling tracking is an explicit operator action (or a
scheduled capture opening its window); nothing here moves the dish otherwise.

The re-point decision is a pure function (:func:`needs_repoint`) over the source's
current az/el and the last commanded az/el, so it is unit-testable without a
rotator; :func:`tracking_tick` wraps it with the IO (pointing, the slew, and
timeline logging) and :func:`tracking_loop` runs it on the app lifespan.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime

from fastapi import FastAPI
from sqlmodel import Session

from jansky_observe.astro.pointing import hpbw_deg
from jansky_observe.astro.stellarium import angular_separation_deg
from jansky_observe.models import Observation, RadioSource, utcnow
from jansky_observe.server.rotator import (
    RotatorError,
    RotatorUnconfigured,
    SlewOutOfLimits,
    slew,
)
from jansky_observe.server.routers import default_location, default_station, source_pointing

__all__ = [
    "REPOINT_FRACTION",
    "TRACK_TICK_S",
    "TrackingState",
    "disable_tracking",
    "enable_tracking",
    "needs_repoint",
    "repoint_threshold_deg",
    "tracking_loop",
    "tracking_tick",
]

logger = logging.getLogger(__name__)

#: Re-point once the source drifts past this fraction of the beam HPBW.
REPOINT_FRACTION = 0.2
#: Seconds between tracking evaluations.
TRACK_TICK_S = 30.0


@dataclass
class TrackingState:
    """What the tracker is doing right now (lives on ``app.state.tracking``)."""

    enabled: bool = False
    observation_id: int | None = None
    source_id: int | None = None
    last_az_deg: float | None = None
    last_el_deg: float | None = None
    note: str = ""


def repoint_threshold_deg(beam_hpbw_deg: float, fraction: float = REPOINT_FRACTION) -> float:
    """The drift (deg) that triggers a re-point — a fraction of the beam, min 0.1°."""
    return max(0.1, beam_hpbw_deg * fraction)


def needs_repoint(
    source_az_deg: float,
    source_el_deg: float,
    last_az_deg: float | None,
    last_el_deg: float | None,
    threshold_deg: float,
) -> bool:
    """Whether the source has drifted far enough from the last command to re-point.

    ``True`` on the first evaluation (no previous command), then whenever the
    angular separation between the source and the last commanded az/el exceeds
    ``threshold_deg``.
    """
    if last_az_deg is None or last_el_deg is None:
        return True
    separation = angular_separation_deg(source_az_deg, source_el_deg, last_az_deg, last_el_deg)
    return separation > threshold_deg


def enable_tracking(app: FastAPI, observation_id: int, source_id: int) -> None:
    """Turn tracking on for a running observation (fresh state, forces a re-point)."""
    app.state.tracking = TrackingState(
        enabled=True, observation_id=observation_id, source_id=source_id, note="tracking enabled"
    )


def disable_tracking(app: FastAPI) -> None:
    """Turn tracking off (idempotent)."""
    state: TrackingState = app.state.tracking
    state.enabled = False
    state.note = "tracking stopped"


def _log(session: Session, observation: Observation, text: str) -> None:
    stamp = utcnow().strftime("%Y-%m-%d %H:%M UTC")
    observation.notes = f"{observation.notes.rstrip()}\n\n[{stamp}] {text}".strip()
    observation.updated_at = utcnow()
    session.add(observation)
    session.commit()


async def tracking_tick(app: FastAPI, now: datetime | None = None) -> None:
    """Evaluate tracking once: re-point if the source has drifted past the beam.

    Auto-stops when the observation is no longer running. A slew that lands
    outside the limits is logged once and held (not disabled) — the source may
    re-enter the envelope; a transport error disables tracking.
    """
    state: TrackingState = app.state.tracking
    engine = app.state.engine
    if not state.enabled or engine is None or state.observation_id is None:
        return
    when = utcnow() if now is None else now

    with Session(engine) as session:
        observation = session.get(Observation, state.observation_id)
        if observation is None or observation.status != "running":
            state.enabled = False
            state.note = "tracking auto-stopped (observation not running)"
            return
        source = session.get(RadioSource, state.source_id) if state.source_id else None
        if source is None:
            state.enabled = False
            state.note = "tracking stopped (source missing)"
            return
        station = default_station(session)
        location = default_location(session)
        pointing = source_pointing(source, location, station, when=when, full=False)
        az_deg, el_deg = float(pointing["az_deg"]), float(pointing["el_deg"])
        threshold = repoint_threshold_deg(hpbw_deg(station.dish_diameter_m))
        if not needs_repoint(az_deg, el_deg, state.last_az_deg, state.last_el_deg, threshold):
            return
        try:
            await asyncio.to_thread(slew, station, az_deg, el_deg)
        except SlewOutOfLimits:
            if state.note != "out-of-limits":
                _log(
                    session,
                    observation,
                    f"Tracking: {source.name} left the slew envelope; holding.",
                )
                state.note = "out-of-limits"
            return
        except (RotatorUnconfigured, RotatorError) as exc:
            state.enabled = False
            state.note = "tracking stopped (rotator error)"
            _log(session, observation, f"Tracking stopped: rotator error ({exc}).")
            return
        state.last_az_deg, state.last_el_deg = az_deg, el_deg
        state.note = "tracking"
        _log(
            session,
            observation,
            f"Tracking re-point to {source.name} — az {az_deg:.1f}° / el {el_deg:.1f}°.",
        )


async def tracking_loop(app: FastAPI) -> None:
    """Run :func:`tracking_tick` every :data:`TRACK_TICK_S`; never dies on error."""
    while True:
        try:
            await tracking_tick(app)
        except Exception:  # noqa: BLE001 - a tracking loop must survive any tick error
            logger.exception("tracking tick failed")
        await asyncio.sleep(TRACK_TICK_S)
