"""Tests for the gpsd client (``jansky_observe.gps``) and the from-gps routes.

A fake gpsd (threaded TCP server speaking the JSON wire protocol: VERSION
banner, ``?WATCH`` ack, then canned reports) stands in for the optional GPS
hardware — no dongle, no gpsd install.
"""

from __future__ import annotations

import json
import socket
import threading
from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlmodel import Session, select

from jansky_observe.config import Settings
from jansky_observe.db import init_db
from jansky_observe.gps import GpsUnavailable, read_fix
from jansky_observe.models import Location
from jansky_observe.server.app import create_app

DEAD_ENDPOINT = "tcp://127.0.0.1:1"

SKY: dict[str, Any] = {"class": "SKY", "satellites": []}
TPV_MODE1: dict[str, Any] = {"class": "TPV", "mode": 1}
TPV_MODE2: dict[str, Any] = {
    "class": "TPV",
    "mode": 2,
    "lat": 40.5,
    "lon": -75.25,
    "time": "2026-07-12T03:04:05.000Z",
}
TPV_MODE3: dict[str, Any] = {
    "class": "TPV",
    "mode": 3,
    "lat": 40.1234,
    "lon": -75.5678,
    "altMSL": 123.4,
    "time": "2026-07-12T03:04:05.000Z",
}


class FakeGpsd:
    """A minimal gpsd: banner, ``?WATCH`` ack, canned reports, then hold open.

    Holding the connection open after the reports (instead of closing) is what
    lets the mode-1-only test exercise the client's overall deadline.
    """

    def __init__(self, reports: list[dict[str, Any]]) -> None:
        self._reports = reports
        self._server = socket.create_server(("127.0.0.1", 0))
        self.port: int = self._server.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._server.close()
        self._thread.join(timeout=5.0)

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self._server.accept()
            except OSError:  # server socket closed — shut down
                return
            with conn:
                self._handle(conn)

    def _handle(self, conn: socket.socket) -> None:
        conn.settimeout(5.0)
        try:
            conn.sendall(b'{"class":"VERSION","release":"3.25"}\n')
            buffer = b""
            while b"\n" not in buffer:  # wait for the ?WATCH command
                chunk = conn.recv(1024)
                if not chunk:
                    return
                buffer += chunk
            conn.sendall(b'{"class":"WATCH","enable":true,"json":true}\n')
            for report in self._reports:
                conn.sendall(json.dumps(report).encode() + b"\n")
            while conn.recv(1024):  # hold open until the client gives up/closes
                pass
        except OSError:
            return


@pytest.fixture()
def gpsd(request: pytest.FixtureRequest) -> Callable[[list[dict[str, Any]]], FakeGpsd]:
    """Factory for fake gpsd servers, all closed at teardown."""

    def make(reports: list[dict[str, Any]]) -> FakeGpsd:
        server = FakeGpsd(reports)
        request.addfinalizer(server.close)
        return server

    return make


def closed_port() -> int:
    """A port with nothing listening on it (bound once, then released)."""
    with socket.create_server(("127.0.0.1", 0)) as server:
        return int(server.getsockname()[1])


GpsdFactory = Callable[[list[dict[str, Any]]], FakeGpsd]


# ---- read_fix (protocol client) ---------------------------------------------------


def test_read_fix_mode3(gpsd: GpsdFactory) -> None:
    """Non-TPV and fixless TPV lines are skipped; the mode-3 TPV wins."""
    server = gpsd([SKY, TPV_MODE1, TPV_MODE3])
    fix = read_fix(port=server.port)
    assert fix.lat_deg == pytest.approx(40.1234)
    assert fix.lon_deg == pytest.approx(-75.5678)
    assert fix.elevation_m == pytest.approx(123.4)
    assert fix.mode == 3
    assert fix.time_utc == "2026-07-12T03:04:05.000Z"


def test_read_fix_mode2_has_no_elevation(gpsd: GpsdFactory) -> None:
    server = gpsd([TPV_MODE2])
    fix = read_fix(port=server.port)
    assert fix.mode == 2
    assert fix.lat_deg == pytest.approx(40.5)
    assert fix.elevation_m is None


def test_read_fix_mode1_only_hits_deadline(gpsd: GpsdFactory) -> None:
    """A gpsd with no satellite lock (mode 1 forever) → GpsUnavailable at deadline."""
    server = gpsd([TPV_MODE1])
    with pytest.raises(GpsUnavailable, match=f"gpsd at 127.0.0.1:{server.port}"):
        read_fix(port=server.port, timeout_s=0.3)


def test_read_fix_connection_refused() -> None:
    port = closed_port()
    with pytest.raises(GpsUnavailable, match=f"gpsd at 127.0.0.1:{port}"):
        read_fix(port=port, timeout_s=0.5)


# ---- routes -----------------------------------------------------------------------


@pytest.fixture()
def engine(tmp_path) -> Engine:
    """A fully migrated + seeded database in a tmp dir."""
    return init_db(tmp_path)


@pytest.fixture()
def client(engine: Engine, tmp_path) -> TestClient:
    """A TestClient over an app with an injected engine."""
    settings = Settings(zmq_endpoint=DEAD_ENDPOINT, data_dir=str(tmp_path))
    return TestClient(create_app(settings, engine=engine))


def _point_env_at(monkeypatch: pytest.MonkeyPatch, port: int) -> None:
    monkeypatch.setenv("JANSKY_OBSERVE_GPSD", f"127.0.0.1:{port}")


def test_api_from_gps_creates_location(
    client: TestClient, engine: Engine, gpsd: GpsdFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    _point_env_at(monkeypatch, gpsd([TPV_MODE3]).port)
    resp = client.post("/api/locations/from-gps")
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "GPS fix 2026-07-12"
    assert data["source"] == "gps"
    assert data["is_default"] is False
    assert data["lat_deg"] == pytest.approx(40.1234)
    assert data["elevation_m"] == pytest.approx(123.4)
    assert data["fix_mode"] == 3
    with Session(engine) as session:
        row = session.exec(select(Location).where(Location.name == "GPS fix 2026-07-12")).one()
        assert row.source == "gps"


def test_api_from_gps_updates_in_place(
    client: TestClient, engine: Engine, gpsd: GpsdFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    with Session(engine) as session:
        location = Location(name="Field site", lat_deg=0.0, lon_deg=0.0, elevation_m=1.0)
        session.add(location)
        session.commit()
        session.refresh(location)
        location_id = location.id
    _point_env_at(monkeypatch, gpsd([TPV_MODE3]).port)
    resp = client.post("/api/locations/from-gps", json={"update_id": location_id})
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == location_id
    assert data["name"] == "Field site"  # update keeps the name
    assert data["lat_deg"] == pytest.approx(40.1234)
    assert data["elevation_m"] == pytest.approx(123.4)
    assert data["source"] == "gps"


def test_api_from_gps_mode2_keeps_elevation(
    client: TestClient, engine: Engine, gpsd: GpsdFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 2D fix (no altitude) must not zero out a stored elevation."""
    with Session(engine) as session:
        location = Location(name="Field site", lat_deg=0.0, lon_deg=0.0, elevation_m=200.0)
        session.add(location)
        session.commit()
        session.refresh(location)
        location_id = location.id
    _point_env_at(monkeypatch, gpsd([TPV_MODE2]).port)
    resp = client.post("/api/locations/from-gps", json={"update_id": location_id})
    assert resp.status_code == 200
    data = resp.json()
    assert data["lat_deg"] == pytest.approx(40.5)
    assert data["elevation_m"] == pytest.approx(200.0)


def test_api_from_gps_503_when_gpsd_down(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _point_env_at(monkeypatch, closed_port())
    resp = client.post("/api/locations/from-gps")
    assert resp.status_code == 503
    assert "gpsd" in resp.json()["detail"]


def test_html_from_gps_fragment(
    client: TestClient, gpsd: GpsdFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    _point_env_at(monkeypatch, gpsd([TPV_MODE3]).port)
    resp = client.post("/catalog/locations/from-gps", data={})
    assert resp.status_code == 200
    assert "GPS fix 2026-07-12" in resp.text
    assert "3D fix" in resp.text


def test_html_from_gps_inline_error(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """gpsd down → the htmx partial carries the friendly error inline."""
    _point_env_at(monkeypatch, closed_port())
    resp = client.post("/catalog/locations/from-gps", data={})
    assert resp.status_code == 200  # htmx only swaps 2xx bodies
    assert 'class="error"' in resp.text
    assert "gpsd" in resp.text


def test_locations_page_renders_gps_button(client: TestClient) -> None:
    body = client.get("/catalog/locations").text
    assert "Use GPS fix" in body
    assert 'hx-post="/catalog/locations/from-gps"' in body
    assert "gpsd" in body  # the optional-hardware note
