"""Offline tests for the weather providers using httpx.MockTransport fixtures."""

from __future__ import annotations

import json

import httpx
import pytest

from jansky_observe.weather import WeatherUnavailable, get_weather
from jansky_observe.weather.nws import NWSProvider, clear_points_cache
from jansky_observe.weather.open_meteo import OpenMeteoProvider

LAT, LON = 40.02, -75.16

# --- NWS fixtures (minimal slices of the real api.weather.gov shapes) ---

NWS_POINTS = {
    "properties": {
        "forecastHourly": "https://api.weather.gov/gridpoints/PHI/50,76/forecast/hourly",
        "observationStations": "https://api.weather.gov/gridpoints/PHI/50,76/stations",
    }
}

NWS_STATIONS = {
    "features": [
        {"id": "https://api.weather.gov/stations/KPHL", "properties": {"stationIdentifier": "KPHL"}}
    ]
}

NWS_LATEST_OBS = {
    "properties": {
        "timestamp": "2026-07-12T13:54:00+00:00",
        "textDescription": "Partly Cloudy",
        "temperature": {"unitCode": "wmoUnit:degC", "value": 27.8},
        "windDirection": {"unitCode": "wmoUnit:degree_(angle)", "value": 320},
        "windSpeed": {"unitCode": "wmoUnit:km_h-1", "value": 18.0},
        "relativeHumidity": {"unitCode": "wmoUnit:percent", "value": 55.5},
    }
}

NWS_HOURLY = {
    "properties": {
        "periods": [
            {
                "startTime": "2026-07-12T14:00:00-04:00",
                "temperature": 86,
                "temperatureUnit": "F",
                "windSpeed": "10 mph",
                "windDirection": "NW",
                "shortForecast": "Mostly Sunny",
                "probabilityOfPrecipitation": {"value": 20},
                "relativeHumidity": {"value": 50},
            },
            {
                "startTime": "2026-07-12T15:00:00-04:00",
                "temperature": 87,
                "temperatureUnit": "F",
                "windSpeed": "5 to 10 mph",
                "windDirection": "W",
                "shortForecast": "Mostly Sunny",
                "probabilityOfPrecipitation": {"value": 25},
                "relativeHumidity": {"value": 48},
            },
            {
                "startTime": "2026-07-12T16:00:00-04:00",
                "temperature": 85,
                "temperatureUnit": "F",
                "windSpeed": "10 mph",
                "windDirection": "W",
                "shortForecast": "Chance Showers",
                "probabilityOfPrecipitation": {"value": 45},
                "relativeHumidity": {"value": 55},
            },
            {
                "startTime": "2026-07-12T17:00:00-04:00",
                "temperature": 83,
                "temperatureUnit": "F",
                "windSpeed": "10 mph",
                "windDirection": "SW",
                "shortForecast": "Showers",
                "probabilityOfPrecipitation": {"value": 60},
                "relativeHumidity": {"value": 60},
            },
        ]
    }
}

# --- Open-Meteo fixture (real response shape, trimmed) ---

OPEN_METEO = {
    "current": {
        "time": "2026-07-12T14:00",
        "temperature_2m": 29.1,
        "relative_humidity_2m": 52,
        "wind_speed_10m": 3.4,
        "wind_direction_10m": 315,
        "cloud_cover": 40,
    },
    "hourly": {
        "time": [
            "2026-07-12T13:00",
            "2026-07-12T14:00",
            "2026-07-12T15:00",
            "2026-07-12T16:00",
            "2026-07-12T17:00",
        ],
        "temperature_2m": [28.0, 29.1, 29.8, 29.5, 28.7],
        "relative_humidity_2m": [55, 52, 50, 51, 54],
        "wind_speed_10m": [3.0, 3.4, 3.9, 4.1, 3.6],
        "wind_direction_10m": [310, 315, 320, 300, 290],
        "cloud_cover": [30, 40, 55, 70, 80],
        "precipitation_probability": [10, 15, 25, 45, 60],
    },
}


@pytest.fixture(autouse=True)
def _fresh_points_cache():
    clear_points_cache()
    yield
    clear_points_cache()


def nws_handler(counts: dict[str, int] | None = None):
    """MockTransport handler serving the NWS fixtures; optionally counts calls by path."""

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if counts is not None:
            counts[path] = counts.get(path, 0) + 1
        if path.startswith("/points/"):
            return httpx.Response(200, json=NWS_POINTS)
        if path.endswith("/stations"):
            return httpx.Response(200, json=NWS_STATIONS)
        if path.endswith("/observations/latest"):
            return httpx.Response(200, json=NWS_LATEST_OBS)
        if path.endswith("/forecast/hourly"):
            return httpx.Response(200, json=NWS_HOURLY)
        return httpx.Response(404, json={"detail": f"unexpected path {path}"})

    return handle


def open_meteo_handler(request: httpx.Request) -> httpx.Response:
    assert request.url.host == "api.open-meteo.com"
    return httpx.Response(200, json=OPEN_METEO)


def failing_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(500, json={"detail": "boom"})


def make_client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


class TestNWS:
    def test_current_parses_latest_observation(self) -> None:
        provider = NWSProvider(client=make_client(nws_handler()))
        now = provider.current(LAT, LON)
        assert now.temp_c == pytest.approx(27.8)
        assert now.wind_speed_ms == pytest.approx(5.0)  # 18 km/h → 5 m/s
        assert now.wind_dir_deg == pytest.approx(320.0)
        assert now.humidity_pct == pytest.approx(55.5)
        assert now.summary == "Partly Cloudy"
        assert now.time is not None and now.time.isoformat().startswith("2026-07-12T13:54")

    def test_hourly_converts_units(self) -> None:
        provider = NWSProvider(client=make_client(nws_handler()))
        hours = provider.hourly_forecast(LAT, LON, 3)
        assert len(hours) == 3
        first = hours[0]
        assert first.temp_c == pytest.approx((86 - 32) * 5 / 9)  # 30.0 °C
        assert first.wind_speed_ms == pytest.approx(10 * 0.44704)
        assert first.wind_dir_deg == pytest.approx(315.0)  # NW
        assert first.summary == "Mostly Sunny"
        assert first.precip_prob_pct == pytest.approx(20.0)
        # "5 to 10 mph" parses its first number.
        assert hours[1].wind_speed_ms == pytest.approx(5 * 0.44704)

    def test_points_cached_across_calls(self) -> None:
        counts: dict[str, int] = {}
        provider = NWSProvider(client=make_client(nws_handler(counts)))
        provider.current(LAT, LON)
        provider.hourly_forecast(LAT, LON, 3)
        provider.current(LAT, LON)
        points_calls = sum(n for path, n in counts.items() if path.startswith("/points/"))
        assert points_calls == 1


class TestOpenMeteo:
    def test_current(self) -> None:
        provider = OpenMeteoProvider(client=make_client(open_meteo_handler))
        now = provider.current(LAT, LON)
        assert now.temp_c == pytest.approx(29.1)
        assert now.wind_speed_ms == pytest.approx(3.4)
        assert now.wind_dir_deg == pytest.approx(315.0)
        assert now.sky_cover_pct == pytest.approx(40.0)
        assert now.humidity_pct == pytest.approx(52.0)

    def test_hourly_starts_at_current_hour(self) -> None:
        provider = OpenMeteoProvider(client=make_client(open_meteo_handler))
        hours = provider.hourly_forecast(LAT, LON, 3)
        assert len(hours) == 3
        # current.time is 14:00, so the 13:00 entry is skipped.
        assert hours[0].time is not None and hours[0].time.hour == 14
        assert hours[0].temp_c == pytest.approx(29.1)
        assert hours[2].precip_prob_pct == pytest.approx(45.0)


class TestGetWeather:
    def test_prefers_nws(self) -> None:
        providers = (
            NWSProvider(client=make_client(nws_handler())),
            OpenMeteoProvider(client=make_client(open_meteo_handler)),
        )
        snapshot = get_weather(LAT, LON, hours=3, providers=providers)
        assert snapshot["provider"] == "nws"
        assert snapshot["now"]["temp_c"] == pytest.approx(27.8)
        assert len(snapshot["hourly"]) == 3
        # The snapshot dict is the Observation.weather_snapshot format → JSON-able.
        json.dumps(snapshot)

    def test_falls_back_to_open_meteo(self) -> None:
        providers = (
            NWSProvider(client=make_client(failing_handler)),
            OpenMeteoProvider(client=make_client(open_meteo_handler)),
        )
        snapshot = get_weather(LAT, LON, hours=3, providers=providers)
        assert snapshot["provider"] == "open_meteo"
        assert snapshot["now"]["temp_c"] == pytest.approx(29.1)
        json.dumps(snapshot)

    def test_both_fail_raises(self) -> None:
        providers = (
            NWSProvider(client=make_client(failing_handler)),
            OpenMeteoProvider(client=make_client(failing_handler)),
        )
        with pytest.raises(WeatherUnavailable, match="(?s)nws.*open_meteo"):
            get_weather(LAT, LON, providers=providers)
