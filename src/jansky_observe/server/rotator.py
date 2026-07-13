"""Station → rotator glue: build the configured client, enforce the limits (M9).

The transports in :mod:`jansky_observe.astro.rotator` are deliberately thin and
model-free. This module is the bridge to the :class:`~jansky_observe.models.Station`
record: it constructs the configured :class:`~jansky_observe.astro.rotator.Rotator`
and applies the station's hard az/el slew envelope and park position — the safety
layer every slew (HTML or the guarded MCP verb) goes through.
"""

from __future__ import annotations

from jansky_observe.astro.rotator import Rotator, RotatorPosition, make_rotator, within_limits
from jansky_observe.models import Station

__all__ = [
    "park_position",
    "rotator_from_station",
    "station_allows",
]


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
