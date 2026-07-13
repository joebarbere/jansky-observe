"""Station → rotator glue: build the configured client, enforce the limits (M9).

The transports in :mod:`jansky_observe.astro.rotator` are deliberately thin and
model-free. This module is the bridge to the :class:`~jansky_observe.models.Station`
record: it constructs the configured :class:`~jansky_observe.astro.rotator.Rotator`
and applies the station's hard az/el slew envelope and park position — the safety
layer every slew (HTML or the guarded MCP verb) goes through.
"""

from __future__ import annotations

from jansky_observe.astro.rotator import (
    Rotator,
    RotatorError,
    RotatorPosition,
    make_rotator,
    within_limits,
)
from jansky_observe.models import Station

__all__ = [
    "RotatorError",
    "RotatorUnconfigured",
    "SlewOutOfLimits",
    "park_position",
    "rotator_from_station",
    "slew",
    "station_allows",
    "stop",
]


class SlewOutOfLimits(RuntimeError):
    """A requested az/el target is outside the station's slew envelope."""


class RotatorUnconfigured(RuntimeError):
    """A control action was requested but the station has no rotator (kind ``none``)."""


def rotator_from_station(station: Station) -> Rotator | None:
    """Build the rotator client for a station, or ``None`` when unconfigured."""
    return make_rotator(
        station.rotator_kind,
        host=station.rotator_host,
        port=station.rotator_port,
        serial_device=station.rotator_serial,
        baud=station.rotator_baud,
    )


def station_allows(station: Station, az_deg: float, el_deg: float) -> bool:
    """Whether an az/el target is inside the station's configured slew envelope."""
    return within_limits(
        az_deg,
        el_deg,
        az_min=station.az_min_deg,
        az_max=station.az_max_deg,
        el_min=station.el_min_deg,
        el_max=station.el_max_deg,
    )


def park_position(station: Station) -> RotatorPosition:
    """The station's stow/park az/el (default straight up, el 90°)."""
    return RotatorPosition(station.park_az_deg, station.park_el_deg)


def slew(station: Station, az_deg: float, el_deg: float) -> None:
    """Limit-check and command a slew — the single slew primitive.

    Every mover (the HTML routes, the tracking loop, and the guarded MCP verb)
    goes through here, so the az/el envelope is enforced in exactly one place.

    Raises
    ------
    SlewOutOfLimits
        The target is outside the station's az/el limits.
    RotatorUnconfigured
        The station has no rotator (kind ``none``).
    RotatorError
        A transport/protocol failure talking to the Drive.
    """
    if not station_allows(station, az_deg, el_deg):
        raise SlewOutOfLimits(
            f"az/el {az_deg:.1f}°/{el_deg:.1f}° is outside the station slew limits "
            f"(az {station.az_min_deg:.0f}–{station.az_max_deg:.0f}°, "
            f"el {station.el_min_deg:.0f}–{station.el_max_deg:.0f}°)"
        )
    rotator = rotator_from_station(station)
    if rotator is None:
        raise RotatorUnconfigured("no rotator configured on this station")
    try:
        rotator.set_position(az_deg, el_deg)
    finally:
        rotator.close()


def stop(station: Station) -> None:
    """Halt rotator motion immediately.

    Raises
    ------
    RotatorUnconfigured
        The station has no rotator.
    RotatorError
        A transport/protocol failure.
    """
    rotator = rotator_from_station(station)
    if rotator is None:
        raise RotatorUnconfigured("no rotator configured on this station")
    try:
        rotator.stop()
    finally:
        rotator.close()
