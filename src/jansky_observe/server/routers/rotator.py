"""Rotator control endpoints (roadmap M9 piece 2) — manual slew + readback.

HTML-only operator actions on top of the M9 piece-1 clients. Every slew is
**limit-checked** against the station's az/el envelope (:func:`station_allows`)
and **logged to the running observation's timeline**; ``GET /api/rotator`` serves
the live, best-effort status (position readback, limits, park) that the station
panel polls and the piece-4 ``get_rotator_status`` MCP tool will proxy.

No MCP verbs here — the guarded ``slew_rotator`` MCP verb is piece 4. Slews are
deliberate operator actions only; nothing here moves the dish on its own.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Form, HTTPException
from fastapi.responses import RedirectResponse
from sqlmodel import col, select

from jansky_observe.astro.rotator import RotatorError
from jansky_observe.models import Observation, RadioSource, Station, utcnow
from jansky_observe.server.rotator import (
    park_position,
    rotator_from_station,
    station_allows,
)
from jansky_observe.server.routers import (
    SessionDep,
    default_location,
    default_station,
    get_or_404,
    source_pointing,
)

__all__ = ["router"]

router = APIRouter(tags=["rotator"])


def _see_other(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def _running_observation(session: SessionDep) -> Observation | None:
    """The currently running observation (for slew logging), or ``None``."""
    return session.exec(
        select(Observation)
        .where(Observation.status == "running")
        .order_by(col(Observation.id).desc())
    ).first()


def _log_slew(session: SessionDep, text: str) -> None:
    """Append a timestamped slew line to the running observation's notes, if any.

    Every slew is logged to the observation timeline (the M9 safety rule). Manual
    slews with no session running are still executed — there is just nothing to
    log them against.
    """
    observation = _running_observation(session)
    if observation is None:
        return
    stamp = utcnow().strftime("%Y-%m-%d %H:%M UTC")
    observation.notes = f"{observation.notes.rstrip()}\n\n[{stamp}] {text}".strip()
    observation.updated_at = utcnow()
    session.add(observation)
    session.commit()


def _require_rotator(station: Station) -> Any:
    """Build the configured rotator or 409 when the station has none."""
    try:
        rotator = rotator_from_station(station)
    except RotatorError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if rotator is None:
        raise HTTPException(status_code=409, detail="no rotator configured on this station")
    return rotator


def _checked_slew(session: SessionDep, station: Station, az_deg: float, el_deg: float) -> None:
    """Limit-check then command a slew; 422 outside the envelope, 502 on transport."""
    if not station_allows(station, az_deg, el_deg):
        raise HTTPException(
            status_code=422,
            detail=(
                f"az/el {az_deg:.1f}°/{el_deg:.1f}° is outside the station slew limits "
                f"(az {station.az_min_deg:.0f}–{station.az_max_deg:.0f}°, "
                f"el {station.el_min_deg:.0f}–{station.el_max_deg:.0f}°)"
            ),
        )
    rotator = _require_rotator(station)
    try:
        rotator.set_position(az_deg, el_deg)
    except RotatorError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    finally:
        rotator.close()


@router.get("/api/rotator")
def api_rotator_status(session: SessionDep) -> dict[str, Any]:
    """Best-effort rotator status: kind, configured?, live az/el readback, limits,
    park. ``reachable`` is ``False`` (with ``error``) when the Drive can't be
    talked to; never raises for an unreachable rotator. Read-only."""
    station = default_station(session)
    park = park_position(station)
    status: dict[str, Any] = {
        "kind": station.rotator_kind,
        "configured": station.rotator_kind != "none",
        "reachable": False,
        "position": None,
        "error": None,
        "limits": {
            "az_min": station.az_min_deg,
            "az_max": station.az_max_deg,
            "el_min": station.el_min_deg,
            "el_max": station.el_max_deg,
        },
        "park": {"az_deg": park.az_deg, "el_deg": park.el_deg},
    }
    try:
        rotator = rotator_from_station(station)
    except RotatorError as exc:
        status["error"] = str(exc)
        return status
    if rotator is None:
        return status
    try:
        position = rotator.get_position()
        status["reachable"] = True
        status["position"] = {"az_deg": position.az_deg, "el_deg": position.el_deg}
    except RotatorError as exc:
        status["error"] = str(exc)
    finally:
        rotator.close()
    return status


@router.post("/rotator/slew")
def rotator_slew(
    session: SessionDep,
    az_deg: Annotated[float, Form()],
    el_deg: Annotated[float, Form()],
) -> RedirectResponse:
    """Manual az/el slew (HTML). Limit-checked; logged to the running observation."""
    station = default_station(session)
    _checked_slew(session, station, az_deg, el_deg)
    _log_slew(session, f"Rotator slew to az {az_deg:.1f}° / el {el_deg:.1f}° (manual).")
    return _see_other("/station")


@router.post("/rotator/stop")
def rotator_stop(session: SessionDep) -> RedirectResponse:
    """Halt rotator motion immediately (HTML)."""
    station = default_station(session)
    rotator = _require_rotator(station)
    try:
        rotator.stop()
    except RotatorError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    finally:
        rotator.close()
    _log_slew(session, "Rotator STOP (manual).")
    return _see_other("/station")


@router.post("/rotator/park")
def rotator_park(session: SessionDep) -> RedirectResponse:
    """Slew to the station's park/stow position (HTML)."""
    station = default_station(session)
    park = park_position(station)
    _checked_slew(session, station, park.az_deg, park.el_deg)
    _log_slew(session, f"Rotator PARK to az {park.az_deg:.1f}° / el {park.el_deg:.1f}° (manual).")
    return _see_other("/station")


@router.post("/observations/{obs_id}/slew_to_target")
def slew_to_target(session: SessionDep, obs_id: int) -> RedirectResponse:
    """Slew to the observation's source at its current computed az/el (offsets
    applied), logging the slew to that observation's timeline (HTML)."""
    observation = get_or_404(session, Observation, obs_id)
    source = get_or_404(session, RadioSource, observation.source_id)
    station = default_station(session)
    pointing = source_pointing(source, default_location(session), station, full=False)
    az_deg, el_deg = float(pointing["az_deg"]), float(pointing["el_deg"])
    if not station_allows(station, az_deg, el_deg):
        raise HTTPException(
            status_code=422,
            detail=(
                f"{source.name} is at az/el {az_deg:.1f}°/{el_deg:.1f}° — outside the slew limits"
            ),
        )
    rotator = _require_rotator(station)
    try:
        rotator.set_position(az_deg, el_deg)
    except RotatorError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    finally:
        rotator.close()
    stamp = utcnow().strftime("%Y-%m-%d %H:%M UTC")
    observation.notes = (
        f"{observation.notes.rstrip()}\n\n"
        f"[{stamp}] Rotator slew to {source.name} — az {az_deg:.1f}° / el {el_deg:.1f}° "
        f"(computed, offsets applied)."
    ).strip()
    observation.updated_at = utcnow()
    session.add(observation)
    session.commit()
    return _see_other(f"/observations/{obs_id}")
