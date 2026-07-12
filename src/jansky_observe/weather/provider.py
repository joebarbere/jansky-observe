"""Weather provider protocol and the NWS→Open-Meteo fallback chain (plan §4.4).

Weather is advisory metadata at 21 cm — clouds don't matter; wind on a
700 mm dish and rain on the operator do. Fields a provider cannot supply
are ``None``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "WeatherHour",
    "WeatherNow",
    "WeatherProvider",
    "WeatherUnavailable",
    "get_weather",
]


class WeatherUnavailable(Exception):
    """Raised when every configured weather provider fails."""


@dataclass(frozen=True)
class WeatherNow:
    """Current conditions at the station."""

    time: datetime | None
    temp_c: float | None
    wind_speed_ms: float | None
    wind_dir_deg: float | None
    sky_cover_pct: float | None
    summary: str | None
    humidity_pct: float | None


@dataclass(frozen=True)
class WeatherHour:
    """One hour of forecast."""

    time: datetime | None
    temp_c: float | None
    wind_speed_ms: float | None
    wind_dir_deg: float | None
    sky_cover_pct: float | None
    summary: str | None
    humidity_pct: float | None
    precip_prob_pct: float | None


@runtime_checkable
class WeatherProvider(Protocol):
    """A source of current conditions and hourly forecast."""

    name: str

    def current(self, lat: float, lon: float) -> WeatherNow:
        """Return current conditions at ``lat``/``lon``."""
        ...

    def hourly_forecast(self, lat: float, lon: float, hours: int) -> list[WeatherHour]:
        """Return the next ``hours`` hours of forecast at ``lat``/``lon``."""
        ...


def _jsonable(value: Any) -> Any:
    """Dataclass-dict values → JSON-able (datetimes become ISO strings)."""
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _as_dict(obj: WeatherNow | WeatherHour) -> dict[str, Any]:
    return {k: _jsonable(v) for k, v in asdict(obj).items()}


def get_weather(
    lat: float,
    lon: float,
    hours: int = 3,
    providers: tuple[WeatherProvider, ...] | None = None,
) -> dict[str, Any]:
    """Fetch the weather snapshot, trying NWS first then Open-Meteo.

    Parameters
    ----------
    lat, lon : float
        Station coordinates (degrees).
    hours : int
        Hours of forecast to include (default 3, per the wizard, plan §5.1).
    providers : tuple of WeatherProvider, optional
        Override the provider chain (for tests). Defaults to
        ``(NWSProvider(), OpenMeteoProvider())``.

    Returns
    -------
    dict
        ``{"now": {...}, "hourly": [{...}, ...], "provider": "nws" | "open_meteo"}``
        — plain JSON-able dicts; this dict is the ``Observation.weather_snapshot``
        format.

    Raises
    ------
    WeatherUnavailable
        If every provider fails.
    """
    if providers is None:
        from jansky_observe.weather.nws import NWSProvider
        from jansky_observe.weather.open_meteo import OpenMeteoProvider

        providers = (NWSProvider(), OpenMeteoProvider())

    errors: list[str] = []
    for provider in providers:
        try:
            now = provider.current(lat, lon)
            hourly = provider.hourly_forecast(lat, lon, hours)
        except Exception as exc:  # noqa: BLE001 — any provider failure → next provider
            errors.append(f"{provider.name}: {exc}")
            continue
        return {
            "now": _as_dict(now),
            "hourly": [_as_dict(h) for h in hourly],
            "provider": provider.name,
        }
    raise WeatherUnavailable("all weather providers failed: " + "; ".join(errors))
