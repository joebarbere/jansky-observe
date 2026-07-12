"""NWS api.weather.gov provider (plan §4.4).

Flow: ``/points/{lat},{lon}`` once per location (module-level TTL cache) →
the returned hourly-forecast URL for planning and the observation-stations
URL → latest observation for the at-start snapshot. Free, keyless — just a
descriptive User-Agent.
"""

from __future__ import annotations

import re
import time
from datetime import datetime
from typing import Any

import httpx

from jansky_observe.weather.provider import WeatherHour, WeatherNow

__all__ = ["NWSProvider", "USER_AGENT", "clear_points_cache"]

USER_AGENT = "jansky-observe (github.com/joebarbere/jansky-observe)"
_BASE_URL = "https://api.weather.gov"
_POINTS_TTL_S = 6 * 3600.0
_TIMEOUT_S = 10.0

_MPH_TO_MS = 0.44704

_COMPASS_DEG = {
    "N": 0.0, "NNE": 22.5, "NE": 45.0, "ENE": 67.5,
    "E": 90.0, "ESE": 112.5, "SE": 135.0, "SSE": 157.5,
    "S": 180.0, "SSW": 202.5, "SW": 225.0, "WSW": 247.5,
    "W": 270.0, "WNW": 292.5, "NW": 315.0, "NNW": 337.5,
}  # fmt: skip

# (lat, lon) rounded to 4 dp → (expiry monotonic time, points properties).
_points_cache: dict[tuple[float, float], tuple[float, dict[str, Any]]] = {}


def clear_points_cache() -> None:
    """Empty the module-level ``/points`` cache (used by tests)."""
    _points_cache.clear()


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value)


def _wind_speed_ms(quantity: dict[str, Any] | None) -> float | None:
    """Convert an NWS quantitative-value wind speed to m/s."""
    if not quantity or quantity.get("value") is None:
        return None
    value = float(quantity["value"])
    unit = quantity.get("unitCode", "")
    if "km_h" in unit:
        return value / 3.6
    if "m_s" in unit:
        return value
    return value  # unknown unit: pass through rather than guess


def _parse_wind_speed_text(text: str | None) -> float | None:
    """Parse a forecast wind-speed string like ``"10 mph"`` or ``"5 to 10 mph"``."""
    if not text:
        return None
    match = re.search(r"(\d+(?:\.\d+)?)", text)
    if match is None:
        return None
    return float(match.group(1)) * _MPH_TO_MS


def _quantity(props: dict[str, Any], key: str) -> float | None:
    quantity = props.get(key) or {}
    value = quantity.get("value")
    return None if value is None else float(value)


def _temp_c(period: dict[str, Any]) -> float | None:
    value = period.get("temperature")
    if value is None:
        return None
    if period.get("temperatureUnit") == "F":
        return (float(value) - 32.0) * 5.0 / 9.0
    return float(value)


class NWSProvider:
    """Weather from the US National Weather Service API.

    Parameters
    ----------
    client : httpx.Client, optional
        Injectable HTTP client — every request goes through it, so tests
        pass one built on ``httpx.MockTransport`` and never hit the network.
    """

    name = "nws"

    def __init__(self, client: httpx.Client | None = None) -> None:
        self._client = client or httpx.Client(
            headers={"User-Agent": USER_AGENT, "Accept": "application/geo+json"},
            timeout=_TIMEOUT_S,
        )

    def _get_json(self, url: str) -> dict[str, Any]:
        response = self._client.get(url)
        response.raise_for_status()
        data: dict[str, Any] = response.json()
        return data

    def _points(self, lat: float, lon: float) -> dict[str, Any]:
        """Gridpoint metadata for a location, cached per rounded lat/lon."""
        key = (round(lat, 4), round(lon, 4))
        cached = _points_cache.get(key)
        now = time.monotonic()
        if cached is not None and cached[0] > now:
            return cached[1]
        data = self._get_json(f"{_BASE_URL}/points/{key[0]},{key[1]}")
        props: dict[str, Any] = data["properties"]
        _points_cache[key] = (now + _POINTS_TTL_S, props)
        return props

    def current(self, lat: float, lon: float) -> WeatherNow:
        """Latest observation from the nearest NWS station."""
        points = self._points(lat, lon)
        stations = self._get_json(points["observationStations"])
        station_url: str = stations["features"][0]["id"]
        obs = self._get_json(f"{station_url}/observations/latest")["properties"]
        return WeatherNow(
            time=_parse_time(obs.get("timestamp")),
            temp_c=_quantity(obs, "temperature"),
            wind_speed_ms=_wind_speed_ms(obs.get("windSpeed")),
            wind_dir_deg=_quantity(obs, "windDirection"),
            sky_cover_pct=None,
            summary=obs.get("textDescription"),
            humidity_pct=_quantity(obs, "relativeHumidity"),
        )

    def hourly_forecast(self, lat: float, lon: float, hours: int) -> list[WeatherHour]:
        """Next ``hours`` hours from the gridpoint hourly forecast."""
        points = self._points(lat, lon)
        forecast = self._get_json(points["forecastHourly"])
        periods: list[dict[str, Any]] = forecast["properties"]["periods"]
        result: list[WeatherHour] = []
        for period in periods[:hours]:
            direction = period.get("windDirection")
            result.append(
                WeatherHour(
                    time=_parse_time(period.get("startTime")),
                    temp_c=_temp_c(period),
                    wind_speed_ms=_parse_wind_speed_text(period.get("windSpeed")),
                    wind_dir_deg=_COMPASS_DEG.get(direction) if direction else None,
                    sky_cover_pct=None,
                    summary=period.get("shortForecast"),
                    humidity_pct=_quantity(period, "relativeHumidity"),
                    precip_prob_pct=_quantity(period, "probabilityOfPrecipitation"),
                )
            )
        return result
