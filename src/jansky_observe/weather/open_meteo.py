"""Open-Meteo provider (plan §4.4) — the documented fallback when NWS is down.

Free, keyless, global. One request serves both current conditions and the
hourly forecast; wind is requested in m/s and times in UTC so no unit
juggling is needed.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx

from jansky_observe.weather.provider import WeatherHour, WeatherNow

__all__ = ["OpenMeteoProvider"]

_BASE_URL = "https://api.open-meteo.com/v1/forecast"
_TIMEOUT_S = 10.0

_HOURLY_VARS = (
    "temperature_2m,relative_humidity_2m,wind_speed_10m,wind_direction_10m,"
    "cloud_cover,precipitation_probability"
)
_CURRENT_VARS = "temperature_2m,relative_humidity_2m,wind_speed_10m,wind_direction_10m,cloud_cover"


def _parse_time(value: str | None) -> datetime | None:
    """Parse an Open-Meteo timestamp (naive ISO, UTC because we request timezone=UTC)."""
    if not value:
        return None
    return datetime.fromisoformat(value).replace(tzinfo=UTC)


def _opt_float(value: Any) -> float | None:
    return None if value is None else float(value)


class OpenMeteoProvider:
    """Weather from api.open-meteo.com.

    Parameters
    ----------
    client : httpx.Client, optional
        Injectable HTTP client — every request goes through it, so tests
        pass one built on ``httpx.MockTransport`` and never hit the network.
    """

    name = "open_meteo"

    def __init__(self, client: httpx.Client | None = None) -> None:
        self._client = client or httpx.Client(timeout=_TIMEOUT_S)

    def _fetch(self, lat: float, lon: float) -> dict[str, Any]:
        response = self._client.get(
            _BASE_URL,
            params={
                "latitude": lat,
                "longitude": lon,
                "current": _CURRENT_VARS,
                "hourly": _HOURLY_VARS,
                "wind_speed_unit": "ms",
                "timezone": "UTC",
                "forecast_days": 2,
            },
        )
        response.raise_for_status()
        data: dict[str, Any] = response.json()
        return data

    def current(self, lat: float, lon: float) -> WeatherNow:
        """Current conditions from the ``current`` block."""
        cur = self._fetch(lat, lon)["current"]
        return WeatherNow(
            time=_parse_time(cur.get("time")),
            temp_c=_opt_float(cur.get("temperature_2m")),
            wind_speed_ms=_opt_float(cur.get("wind_speed_10m")),
            wind_dir_deg=_opt_float(cur.get("wind_direction_10m")),
            sky_cover_pct=_opt_float(cur.get("cloud_cover")),
            summary=None,
            humidity_pct=_opt_float(cur.get("relative_humidity_2m")),
        )

    def hourly_forecast(self, lat: float, lon: float, hours: int) -> list[WeatherHour]:
        """Next ``hours`` hours starting at the first hour ≥ the current time."""
        data = self._fetch(lat, lon)
        hourly = data["hourly"]
        times: list[str] = hourly["time"]

        start_idx = 0
        current_time = _parse_time(data.get("current", {}).get("time"))
        if current_time is not None:
            for i, stamp in enumerate(times):
                parsed = _parse_time(stamp)
                if parsed is not None and parsed >= current_time:
                    start_idx = i
                    break

        def col(name: str, i: int) -> float | None:
            values = hourly.get(name)
            if values is None or i >= len(values):
                return None
            return _opt_float(values[i])

        result: list[WeatherHour] = []
        for i in range(start_idx, min(start_idx + hours, len(times))):
            result.append(
                WeatherHour(
                    time=_parse_time(times[i]),
                    temp_c=col("temperature_2m", i),
                    wind_speed_ms=col("wind_speed_10m", i),
                    wind_dir_deg=col("wind_direction_10m", i),
                    sky_cover_pct=col("cloud_cover", i),
                    summary=None,
                    humidity_pct=col("relative_humidity_2m", i),
                    precip_prob_pct=col("precipitation_probability", i),
                )
            )
        return result
