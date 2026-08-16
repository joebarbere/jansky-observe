"""Remote RFI scanner: ingest, presence, status-bar chip, discovery, client.

Synthetic fixtures only (plans/remote-scanner.md): a hand-built
``hackrf_sweep``-format CSV for uploads, a fake ``zeroconf`` module injected
into ``sys.modules`` for discovery, and the ``deploy/remote_scan.py`` client
imported as a module — including the guard test that mirrors
``test_profiles.py``'s bias-tee rule for the client's command builder.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlmodel import Session, select

from jansky_observe.config import Settings
from jansky_observe.db import init_db
from jansky_observe.models import Capture
from jansky_observe.server.app import create_app
from jansky_observe.server.routers.remote_scan import CONNECTED_MAX_AGE_S, scanner_status

DEAD_ENDPOINT = "tcp://127.0.0.1:1"

SWEEP_CSV = "\n".join(
    f"2026-08-15, 12:00:{i:02d}, 1000000000, 1005000000, 1000000, 20, "
    "-70.0, -69.0, -38.0, -70.5, -69.5"
    for i in range(4)
)


@pytest.fixture()
def engine(tmp_path) -> Engine:
    return init_db(tmp_path)


@pytest.fixture()
def client(engine: Engine, tmp_path) -> TestClient:
    settings = Settings(zmq_endpoint=DEAD_ENDPOINT, data_dir=str(tmp_path))
    return TestClient(create_app(settings, engine=engine))


# ---- ingest -----------------------------------------------------------------------


def test_upload_registers_capture_and_file(client: TestClient, engine: Engine, tmp_path):
    resp = client.post(
        "/api/remote_scan/sweep",
        params={"host": "macbook", "antenna": "SRH777CA", "label": "patio", "num_sweeps": 4},
        content=SWEEP_CSV,
        headers={"Content-Type": "text/csv"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["n_rows"] == 4
    path = Path(body["path"])
    assert path.exists()
    assert path.parent == tmp_path / "captures"
    assert path.name.startswith("rfi-") and path.name.endswith("-remote.csv")
    with Session(engine) as session:
        capture = session.exec(select(Capture).where(Capture.id == body["capture_id"])).one()
    assert capture.device == "hackrf-remote"
    assert capture.format == "hackrf_sweep_csv"
    assert capture.sdr_settings["remote"] == {
        "host": "macbook",
        "antenna": "SRH777CA",
        "label": "patio",
    }
    assert capture.sdr_settings["n_rows"] == 4
    # the upload doubles as a heartbeat
    status = client.get("/api/remote_scan/status").json()["scanners"]
    assert status[0]["host"] == "macbook"
    assert status[0]["connected"] is True
    assert status[0]["n_uploads"] == 1


def test_upload_rejects_garbage_and_leaves_no_file(client: TestClient, tmp_path):
    resp = client.post(
        "/api/remote_scan/sweep",
        params={"host": "macbook"},
        content="this is not a sweep\nno,rows,here",
    )
    assert resp.status_code == 422
    assert not list((tmp_path / "captures").glob("*-remote.csv"))
    resp = client.post("/api/remote_scan/sweep", params={"host": "macbook"}, content="")
    assert resp.status_code == 422


# ---- presence ---------------------------------------------------------------------


def test_heartbeat_and_aging(client: TestClient):
    resp = client.post("/api/remote_scan/heartbeat", json={"host": "macbook", "note": "patio run"})
    assert resp.status_code == 200
    row = resp.json()["scanners"][0]
    assert row["host"] == "macbook"
    assert row["connected"] is True
    assert row["note"] == "patio run"


def test_scanner_status_ages_out():
    import time as _time

    stale = {"lost-host": {"last_seen_unix": _time.time() - CONNECTED_MAX_AGE_S - 5}}
    rows = scanner_status(stale)
    assert rows[0]["connected"] is False
    assert scanner_status(None) == []


def test_status_bar_carries_scanner_chip(client: TestClient):
    client.post("/api/remote_scan/heartbeat", json={"host": "macbook"})
    bar = client.get("/api/status_bar").json()
    assert bar["scanner"]["host"] == "macbook"
    assert bar["scanner"]["connected"] is True


def test_status_bar_scanner_none_when_never_seen(client: TestClient):
    assert client.get("/api/status_bar").json()["scanner"] is None


# ---- discovery --------------------------------------------------------------------


def test_advertise_disabled_by_env(monkeypatch):
    from jansky_observe.server import discovery

    monkeypatch.setenv("JANSKY_OBSERVE_NO_MDNS", "1")
    assert discovery.advertise(8000) is None


def test_advertise_and_unregister_with_fake_zeroconf(monkeypatch):
    from jansky_observe.server import discovery

    monkeypatch.delenv("JANSKY_OBSERVE_NO_MDNS", raising=False)
    calls: list[str] = []

    class FakeZeroconf:
        def register_service(self, info) -> None:
            calls.append("register")

        def unregister_service(self, info) -> None:
            calls.append("unregister")

        def close(self) -> None:
            calls.append("close")

    class FakeServiceInfo:
        def __init__(self, *args, **kwargs) -> None:
            calls.append("info")

    fake = types.ModuleType("zeroconf")
    fake.Zeroconf = FakeZeroconf  # type: ignore[attr-defined]
    fake.ServiceInfo = FakeServiceInfo  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "zeroconf", fake)

    handle = discovery.advertise(8000)
    assert handle is not None
    assert calls == ["info", "register"]
    discovery.unregister(handle)
    assert calls == ["info", "register", "unregister", "close"]
    discovery.unregister(None)  # no-op


def test_advertise_survives_zeroconf_failure(monkeypatch):
    from jansky_observe.server import discovery

    monkeypatch.delenv("JANSKY_OBSERVE_NO_MDNS", raising=False)

    class Exploding:
        def __init__(self) -> None:
            raise OSError("no network")

    fake = types.ModuleType("zeroconf")
    fake.Zeroconf = Exploding  # type: ignore[attr-defined]
    fake.ServiceInfo = object  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "zeroconf", fake)
    assert discovery.advertise(8000) is None


# ---- the client (deploy/remote_scan.py, imported as a module) ----------------------


def _load_client() -> types.ModuleType:
    path = Path(__file__).resolve().parent.parent / "deploy" / "remote_scan.py"
    spec = importlib.util.spec_from_file_location("remote_scan_client", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_client_build_cmd_has_no_port_power_parameter():
    """The bias-tee rule, client side (mirrors test_profiles.py): the command
    builder must be structurally incapable of emitting -p."""
    mod = _load_client()
    cmd = mod.build_cmd(1, 6000, 1_000_000, 20)
    assert "-p" not in cmd
    assert cmd[:1] == ["hackrf_sweep"]
    import inspect

    assert "port_power" not in inspect.signature(mod.build_cmd).parameters
    assert "bias" not in str(inspect.signature(mod.build_cmd)).lower()


def test_client_refuses_smuggled_port_power():
    mod = _load_client()
    with pytest.raises(SystemExit) as exc:
        mod.main(["--dry-run", "--sweep-arg=-p"])
    assert "bias-tee" in str(exc.value)


def test_client_resolution_prefers_explicit(monkeypatch):
    mod = _load_client()
    checked: list[str] = []

    def fake_healthz(url: str, timeout_s: float = 3.0) -> bool:
        checked.append(url)
        return url == "http://pi.example:8000"

    monkeypatch.setattr(mod, "_healthz", fake_healthz)
    assert mod.resolve_station("http://pi.example:8000/") == "http://pi.example:8000"
    assert checked == ["http://pi.example:8000"]


def test_client_resolution_falls_through_to_default(monkeypatch):
    mod = _load_client()
    monkeypatch.delenv("JANSKY_STATION_URL", raising=False)
    monkeypatch.setattr(mod, "_healthz", lambda url, timeout_s=3.0: url == mod.DEFAULT_FALLBACK)
    monkeypatch.setattr(mod, "_mdns_browse", lambda timeout_s=4.0: None)
    assert mod.resolve_station(None) == mod.DEFAULT_FALLBACK


def test_client_resolution_exits_when_nothing_answers(monkeypatch):
    mod = _load_client()
    monkeypatch.delenv("JANSKY_STATION_URL", raising=False)
    monkeypatch.setattr(mod, "_healthz", lambda url, timeout_s=3.0: False)
    monkeypatch.setattr(mod, "_mdns_browse", lambda timeout_s=4.0: None)
    with pytest.raises(SystemExit):
        mod.resolve_station(None)
