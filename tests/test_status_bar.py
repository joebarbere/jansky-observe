"""Tests for the cockpit status-bar payload (roadmap M6).

Network + hardware are monkeypatched: ``ctl_request`` (daemon), ``get_weather``
(providers), and ``shutil.disk_usage`` (the volume). LST goes through the real
astropy helper at a fixed instant.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from jansky_observe.db import init_db
from jansky_observe.frames import SpectralFrame
from jansky_observe.server import status_bar as sb
from jansky_observe.weather.provider import WeatherUnavailable

_FIXED_NOW = datetime(2026, 7, 12, 14, 0, 0, tzinfo=UTC).timestamp()


class _FakeBroadcaster:
    def __init__(self, latest: SpectralFrame | None = None, fps: float | None = None) -> None:
        self.latest = latest
        self.fps = fps


def _frame(timestamp: float, rate_hz: float = 3e6) -> SpectralFrame:
    return SpectralFrame(
        seq=1,
        timestamp=timestamp,
        center_freq_hz=1.4204e9,
        sample_rate_hz=rate_hz,
        power_db=np.zeros(64, dtype=np.float32),
    )


def _usage(free: float, total: float) -> SimpleNamespace:
    return SimpleNamespace(free=free, total=total, used=total - free)


# --- LST helper ------------------------------------------------------------


def test_local_sidereal_time_in_range() -> None:
    from jansky_observe.astro.pointing import local_sidereal_time_hours

    when = datetime(2026, 7, 12, 14, 0, 0, tzinfo=UTC)
    lst = local_sidereal_time_hours(-75.211, when)
    assert 0.0 <= lst < 24.0


# --- station chip ----------------------------------------------------------


def test_station_chip_seeded(tmp_path) -> None:
    engine = init_db(tmp_path)
    chip, lat, lon = sb._station_chip(engine)
    assert chip["name"] == "Discovery Dish"
    assert chip["calibrated"] is False  # seeded offsets are 0 → uncalibrated
    assert chip["location"] == "Home"
    assert lat == pytest.approx(40.024)
    assert lon == pytest.approx(-75.211)


def test_station_chip_no_engine() -> None:
    chip, lat, lon = sb._station_chip(None)
    assert chip == {"name": None, "calibrated": False, "location": None}
    assert lat is None and lon is None


# --- source badge ----------------------------------------------------------


def test_source_badge_fresh(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sb, "ctl_request", lambda *a, **k: {"ok": True, "source": "airspy"})
    bc = _FakeBroadcaster(latest=_frame(_FIXED_NOW - 1.0), fps=4.0)
    out = sb._source_badge("tcp://x", bc, now=_FIXED_NOW)
    assert out == {
        "reachable": True,
        "source": "airspy",
        "fps": 4.0,
        "frame_age_s": pytest.approx(1.0),
        "stale": False,
    }


def test_source_badge_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sb, "ctl_request", lambda *a, **k: {"ok": False, "error": "no daemon"})
    out = sb._source_badge("tcp://x", _FakeBroadcaster(), now=_FIXED_NOW)
    assert out["reachable"] is False
    assert out["source"] is None
    assert out["stale"] is True


def test_source_badge_stale_frame(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sb, "ctl_request", lambda *a, **k: {"ok": True, "source": "synthetic"})
    bc = _FakeBroadcaster(latest=_frame(_FIXED_NOW - 60.0), fps=4.0)
    out = sb._source_badge("tcp://x", bc, now=_FIXED_NOW)
    assert out["stale"] is True


# --- disk gauge ------------------------------------------------------------


def test_disk_gauge_hours_from_frame(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sb.shutil, "disk_usage", lambda _p: _usage(500 * sb._GB, 1000 * sb._GB))
    bc = _FakeBroadcaster(latest=_frame(_FIXED_NOW, rate_hz=3e6))
    out = sb._disk_gauge("/data", bc)
    assert out["status"] == "ok"
    # 500 GB / (3e6 * 4 B/s) / 3600 = ~11.57 h
    assert out["sigmf_hours_remaining"] == pytest.approx(11.6, abs=0.2)


def test_disk_gauge_thresholds(monkeypatch: pytest.MonkeyPatch) -> None:
    bc = _FakeBroadcaster()
    monkeypatch.setattr(sb.shutil, "disk_usage", lambda _p: _usage(150 * sb._GB, 1000 * sb._GB))
    assert sb._disk_gauge("/d", bc)["status"] == "warn"
    monkeypatch.setattr(sb.shutil, "disk_usage", lambda _p: _usage(50 * sb._GB, 1000 * sb._GB))
    out = sb._disk_gauge("/d", bc)
    assert out["status"] == "error"
    assert out["sigmf_hours_remaining"] is None  # no frame yet


def test_disk_gauge_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_p: Any) -> Any:
        raise OSError("gone")

    monkeypatch.setattr(sb.shutil, "disk_usage", boom)
    assert sb._disk_gauge("/nope", _FakeBroadcaster())["status"] == "unavailable"


# --- weather chip (caching) ------------------------------------------------


def test_weather_chip_fetches_and_caches(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    def fake_get_weather(lat: float, lon: float, *a: Any, **k: Any) -> dict[str, Any]:
        calls["n"] += 1
        return {"now": {"temp_c": 21.4, "summary": "Clear"}, "provider": "nws"}

    monkeypatch.setattr(sb, "get_weather", fake_get_weather)
    cache = sb.WeatherCache()
    first = sb._weather_chip(cache, 40.0, -75.0, now=_FIXED_NOW)
    assert first == {"temp_c": 21.4, "summary": "Clear", "provider": "nws"}
    # Within the TTL → served from cache, no second provider call.
    sb._weather_chip(cache, 40.0, -75.0, now=_FIXED_NOW + 60.0)
    assert calls["n"] == 1
    # Past the TTL → refetched.
    sb._weather_chip(cache, 40.0, -75.0, now=_FIXED_NOW + sb._WEATHER_TTL_S + 1.0)
    assert calls["n"] == 2


def test_weather_chip_unavailable_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a: Any, **k: Any) -> Any:
        raise WeatherUnavailable("all providers down")

    monkeypatch.setattr(sb, "get_weather", boom)
    out = sb._weather_chip(sb.WeatherCache(), 40.0, -75.0, now=_FIXED_NOW)
    assert out is None


def test_weather_chip_no_location() -> None:
    assert sb._weather_chip(sb.WeatherCache(), None, None, now=_FIXED_NOW) is None


# --- full bundle -----------------------------------------------------------


def test_build_status_bar_integration(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setattr(sb, "ctl_request", lambda *a, **k: {"ok": True, "source": "synthetic"})
    monkeypatch.setattr(
        sb,
        "get_weather",
        lambda *a, **k: {"now": {"temp_c": 19.0, "summary": "Cloudy"}, "provider": "open_meteo"},
    )
    monkeypatch.setattr(sb.shutil, "disk_usage", lambda _p: _usage(300 * sb._GB, 1000 * sb._GB))
    engine = init_db(tmp_path)
    payload = sb.build_status_bar(
        data_dir=str(tmp_path),
        ctl_endpoint="tcp://x",
        broadcaster=_FakeBroadcaster(latest=_frame(_FIXED_NOW - 1.0), fps=4.0),
        engine=engine,
        weather_cache=sb.WeatherCache(),
        now=_FIXED_NOW,
    )
    assert set(payload) == {"server_time_utc", "lst_hours", "station", "source", "weather", "disk"}
    assert payload["station"]["name"] == "Discovery Dish"
    assert 0.0 <= payload["lst_hours"] < 24.0
    assert payload["source"]["source"] == "synthetic"
    assert payload["weather"]["temp_c"] == 19.0
    assert payload["disk"]["status"] == "ok"
