"""Offline tests for the Stellarium RemoteControl client (httpx.MockTransport).

The request-shape assertions encode the researched API contract (plan §4.3,
verified against the RemoteControl API description and the Stellarium 24.1
source): ``POST /api/main/view`` takes ``az``/``alt`` in **radians** with
azimuth counted from **South toward East** (``az' = 180° − az_from_north``);
``GET /api/objects/info?format=json`` reports degrees, azimuth from North.
"""

from __future__ import annotations

import math
from collections.abc import Callable

import httpx
import pytest

from jansky_observe.astro.stellarium import (
    StellariumClient,
    StellariumUnavailable,
    angular_separation_deg,
)

BASE_URL = "http://desktop:8090"


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> StellariumClient:
    return StellariumClient(BASE_URL, client=httpx.Client(transport=httpx.MockTransport(handler)))


def _form(request: httpx.Request) -> httpx.QueryParams:
    """Parse an x-www-form-urlencoded POST body."""
    return httpx.QueryParams(request.read().decode())


# ---- find_object ----------------------------------------------------------------


def test_find_object_returns_best_match_and_all_matches() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/api/objects/find"
        assert request.url.params["str"] == "Cyg A"
        return httpx.Response(200, json=["Cygnus A", "Cygnus X-1"])

    result = _client(handler).find_object("Cyg A")
    assert result == {"name": "Cygnus A", "matches": ["Cygnus A", "Cygnus X-1"]}


def test_find_object_no_match_returns_none() -> None:
    result = _client(lambda request: httpx.Response(200, json=[])).find_object("Cygnus region HI")
    assert result is None


def test_find_object_error_status_returns_none() -> None:
    result = _client(lambda request: httpx.Response(400, text="error")).find_object("x")
    assert result is None


# ---- object_info ----------------------------------------------------------------


def test_object_info_requests_json_format() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/api/objects/info"
        assert request.url.params["name"] == "Cygnus A"
        assert request.url.params["format"] == "json"
        return httpx.Response(200, json={"azimuth": 123.4, "altitude": 56.7})

    info = _client(handler).object_info("Cygnus A")
    assert info["azimuth"] == pytest.approx(123.4)
    assert info["altitude"] == pytest.approx(56.7)


def test_object_info_unknown_object_returns_empty_dict() -> None:
    info = _client(lambda request: httpx.Response(400, text="not found")).object_info("nope")
    assert info == {}


# ---- slew_view: the researched units ---------------------------------------------


def test_slew_view_sends_radians_azimuth_from_south() -> None:
    seen: dict[str, float] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/main/view"
        form = _form(request)
        seen["az"] = float(form["az"])
        seen["alt"] = float(form["alt"])
        return httpx.Response(200, text="ok")

    # astropy convention: az 300° from North, el 45° →
    # Stellarium wants radians with az' = 180° − az (from South toward East).
    _client(handler).slew_view(300.0, 45.0)
    assert seen["az"] == pytest.approx(math.radians(180.0 - 300.0))
    assert seen["alt"] == pytest.approx(math.radians(45.0))


def test_slew_view_north_horizon() -> None:
    seen: dict[str, float] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        form = _form(request)
        seen["az"] = float(form["az"])
        seen["alt"] = float(form["alt"])
        return httpx.Response(200, text="ok")

    _client(handler).slew_view(0.0, 0.0)  # due North → az' = 180°
    assert seen["az"] == pytest.approx(math.pi)
    assert seen["alt"] == pytest.approx(0.0)


def test_slew_view_error_status_raises_unavailable() -> None:
    client = _client(lambda request: httpx.Response(400, text="bad request"))
    with pytest.raises(StellariumUnavailable, match="view slew failed"):
        client.slew_view(120.0, 30.0)


# ---- focus ----------------------------------------------------------------------


@pytest.mark.parametrize(("body", "expected"), [("true", True), ("false", False)])
def test_focus_posts_target_and_parses_bool(body: str, expected: bool) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/main/focus"
        assert _form(request)["target"] == "Cygnus A"
        return httpx.Response(200, text=body)

    assert _client(handler).focus("Cygnus A") is expected


# ---- unavailability --------------------------------------------------------------


def test_connection_error_raises_stellarium_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    client = _client(handler)
    with pytest.raises(StellariumUnavailable, match="unreachable"):
        client.find_object("Sun")
    with pytest.raises(StellariumUnavailable):
        client.object_info("Sun")
    with pytest.raises(StellariumUnavailable):
        client.slew_view(180.0, 45.0)
    with pytest.raises(StellariumUnavailable):
        client.focus("Sun")


def test_base_url_trailing_slash_is_stripped() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == f"{BASE_URL}/api/objects/find?str=Sun"
        return httpx.Response(200, json=["Sun"])

    client = StellariumClient(
        BASE_URL + "/", client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    assert client.find_object("Sun") == {"name": "Sun", "matches": ["Sun"]}


# ---- the cross-check separation ---------------------------------------------------


def test_angular_separation_simple_cases() -> None:
    assert angular_separation_deg(0.0, 0.0, 0.0, 1.0) == pytest.approx(1.0)
    assert angular_separation_deg(10.0, 45.0, 10.0, 45.0) == pytest.approx(0.0, abs=1e-9)
    # near the zenith a 180° azimuth swing is a small great-circle hop
    assert angular_separation_deg(0.0, 89.0, 180.0, 89.0) == pytest.approx(2.0)


def test_angular_separation_beats_naive_delta_az_near_zenith() -> None:
    sep = angular_separation_deg(0.0, 85.0, 10.0, 85.0)
    assert sep < 1.0  # naive Δaz would say 10°
