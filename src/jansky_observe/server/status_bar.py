"""The status-bar payload (roadmap M6 "station cockpit").

One endpoint feeds the bar that rides every page: the clocks (the server's UTC
instant + LST, so the browser can render UTC/local itself and tick LST), the
active station chip (name + Sun-cal pointing offsets), the source badge
(daemon source + fps + frame age), a weather chip (cached ~15 min so the bar
never waits on the network), and the disk gauge (free/total + estimated hours
of SigMF headroom, with the amber-<20 % / red-<10 % thresholds).

Kept out of ``app.py`` so the route stays a thin off-loop wrapper; the builder
takes plain arguments and a caller-owned weather cache, so it unit-tests
without a live app.
"""

from __future__ import annotations

import shutil
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlmodel import Session, select

from jansky_observe.astro.pointing import local_sidereal_time_hours
from jansky_observe.control import ctl_request
from jansky_observe.models import Location, Station
from jansky_observe.weather.provider import WeatherUnavailable, get_weather

if TYPE_CHECKING:  # pragma: no cover
    from sqlalchemy import Engine

    from jansky_observe.server.app import Broadcaster

__all__ = ["WeatherCache", "build_status_bar"]

_CTL_TIMEOUT_MS = 800
_STALE_FRAME_S = 10.0
_WEATHER_TTL_S = 900.0  # ~15 min, per the M6 spec
_WEATHER_FAIL_TTL_S = 120.0  # back off, but don't hammer, when providers fail
_DISK_AMBER_FRACTION = 0.20
_DISK_RED_FRACTION = 0.10
_GB = 1e9
_SECONDS_PER_HOUR = 3600.0
_SIGMF_BYTES_PER_SAMPLE = 4  # ci16_le, the Airspy's native INT16


# The last weather fetch, stashed on ``app.state`` and handed back each call so
# the ~15-min TTL persists across requests: keys ``key`` (lat, lon), ``at``
# (unix seconds), ``value`` (the chip dict or ``None``).
WeatherCache = dict[str, Any]


def _station_chip(
    engine: Engine | None,
) -> tuple[dict[str, Any], float | None, float | None]:
    """The active-station chip plus the default location's lat/lon (for LST + weather)."""
    if engine is None:
        return {"name": None, "calibrated": False, "location": None}, None, None
    with Session(engine) as session:
        station = session.exec(select(Station)).first()
        location = session.exec(
            select(Location).where(Location.is_default == True)  # noqa: E712
        ).first()
    lat = location.lat_deg if location is not None else None
    lon = location.lon_deg if location is not None else None
    location_name = location.name if location is not None else None
    if station is None:
        return {"name": None, "calibrated": False, "location": location_name}, lat, lon
    calibrated = station.pointing_offset_az_deg != 0.0 or station.pointing_offset_el_deg != 0.0
    chip = {
        "name": station.name,
        "offset_az_deg": station.pointing_offset_az_deg,
        "offset_el_deg": station.pointing_offset_el_deg,
        "calibrated": calibrated,
        "location": location_name,
    }
    return chip, lat, lon


def _source_badge(ctl_endpoint: str, broadcaster: Broadcaster, *, now: float) -> dict[str, Any]:
    """Daemon source + fps + frame age (amber when stale/unreachable)."""
    reply = ctl_request(ctl_endpoint, {"cmd": "status"}, timeout_ms=_CTL_TIMEOUT_MS)
    latest = broadcaster.latest
    frame_age_s = None if latest is None else max(0.0, now - latest.timestamp)
    reachable = bool(reply.get("ok"))
    stale = frame_age_s is None or frame_age_s > _STALE_FRAME_S
    return {
        "reachable": reachable,
        "source": reply.get("source") if reachable else None,
        "fps": broadcaster.fps,
        "frame_age_s": frame_age_s,
        "stale": stale or not reachable,
    }


def _disk_gauge(data_dir: str, broadcaster: Broadcaster) -> dict[str, Any]:
    """Free/total + estimated SigMF hours remaining at the current sample rate."""
    try:
        usage = shutil.disk_usage(data_dir)
    except OSError as exc:
        return {"status": "unavailable", "reason": str(exc)}
    fraction_free = usage.free / usage.total if usage.total else 0.0
    if fraction_free < _DISK_RED_FRACTION:
        status = "error"
    elif fraction_free < _DISK_AMBER_FRACTION:
        status = "warn"
    else:
        status = "ok"
    latest = broadcaster.latest
    sigmf_hours = None
    if latest is not None and latest.sample_rate_hz > 0:
        bytes_per_hour = latest.sample_rate_hz * _SIGMF_BYTES_PER_SAMPLE * _SECONDS_PER_HOUR
        sigmf_hours = usage.free / bytes_per_hour
    return {
        "status": status,
        "free_gb": round(usage.free / _GB, 1),
        "total_gb": round(usage.total / _GB, 1),
        "fraction_free": round(fraction_free, 3),
        "sigmf_hours_remaining": None if sigmf_hours is None else round(sigmf_hours, 1),
    }


def _weather_chip(
    cache: WeatherCache, lat: float | None, lon: float | None, *, now: float
) -> dict[str, Any] | None:
    """Cached current conditions (~15 min TTL); ``None`` if unavailable."""
    if lat is None or lon is None:
        return None
    cached = cache.get("value")
    key = cache.get("key")
    ttl = _WEATHER_TTL_S if cached is not None else _WEATHER_FAIL_TTL_S
    if key == (lat, lon) and now - cache.get("at", 0.0) < ttl:
        return cached
    try:
        snapshot = get_weather(lat, lon)
        now_block = snapshot.get("now", {})
        value: dict[str, Any] | None = {
            "temp_c": now_block.get("temp_c"),
            "summary": now_block.get("summary"),
            "provider": snapshot.get("provider"),
        }
    except WeatherUnavailable:
        value = None
    cache["key"] = (lat, lon)
    cache["at"] = now
    cache["value"] = value
    return value


def build_status_bar(
    *,
    data_dir: str,
    ctl_endpoint: str,
    broadcaster: Broadcaster,
    engine: Engine | None,
    weather_cache: WeatherCache,
    now: float | None = None,
) -> dict[str, Any]:
    """Assemble the status-bar payload (roadmap M6).

    Parameters
    ----------
    data_dir : str
        Station data directory (``Settings.data_dir``) — the disk gauge target.
    ctl_endpoint : str
        Capture-daemon control endpoint (``Settings.ctl_endpoint``).
    broadcaster : Broadcaster
        Live-frame fan-out, for fps / frame age / sample rate.
    engine : Engine or None
        Database engine (``None`` before the lifespan opens it).
    weather_cache : WeatherCache
        Caller-owned cache dict (persist one on ``app.state``) so the ~15-min
        TTL survives across requests.
    now : float, optional
        Wall-clock override (unix seconds) for the clocks/ages; current time by
        default. Used by tests.

    Returns
    -------
    dict
        ``server_time_utc``, ``lst_hours``, ``station``, ``source``,
        ``weather``, and ``disk``.
    """
    now = time.time() if now is None else now
    station_chip, lat, lon = _station_chip(engine)
    lst_hours = None if lon is None else local_sidereal_time_hours(lon, _dt(now))
    return {
        "server_time_utc": _dt(now).isoformat(),
        "lst_hours": None if lst_hours is None else round(lst_hours, 4),
        "station": station_chip,
        "source": _source_badge(ctl_endpoint, broadcaster, now=now),
        "weather": _weather_chip(weather_cache, lat, lon, now=now),
        "disk": _disk_gauge(data_dir, broadcaster),
    }


def _dt(now: float) -> datetime:
    return datetime.fromtimestamp(now, tz=UTC)
