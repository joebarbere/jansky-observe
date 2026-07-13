"""The unattended capture scheduler (roadmap M7)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlmodel import Session, select

from jansky_observe.config import Settings
from jansky_observe.db import init_db
from jansky_observe.models import RadioSource, Schedule
from jansky_observe.server import scheduler as sched
from jansky_observe.server.app import create_app
from jansky_observe.server.scheduler import (
    SchedulerState,
    firing_window,
    next_decision,
    projected_capture_bytes,
    would_exceed_disk_red,
)

DEAD_ENDPOINT = "tcp://127.0.0.1:1"
NOW = datetime(2026, 7, 1, 12, 0, 0, tzinfo=UTC)


def _schedule(**kw: Any) -> Schedule:
    base = dict(
        id=1, name="s", source_id=1, lead_min=5.0, run_min=30.0, format="npz", repeat="daily"
    )
    base.update(kw)
    return Schedule(**base)


# --- pure helpers ----------------------------------------------------------


def test_firing_window() -> None:
    start, stop = firing_window(5, 30, NOW)
    assert start == NOW - timedelta(minutes=5)
    assert stop == start + timedelta(minutes=30)


def test_projected_capture_bytes() -> None:
    assert projected_capture_bytes("sigmf", 1, 3e6) == 3e6 * 4 * 60
    assert projected_capture_bytes("npz", 1, 3e6, n_fft=2048, fps=4) == 2048 * 4 * 4 * 60


def test_would_exceed_disk_red() -> None:
    assert would_exceed_disk_red(1e9, 100e9, 0) is True  # 1% free < 10%
    assert would_exceed_disk_red(50e9, 100e9, 0) is False
    assert would_exceed_disk_red(15e9, 100e9, 10e9) is True  # 5% after the write
    assert would_exceed_disk_red(0, 0, 0) is False


def test_next_decision_start_stop_and_guards() -> None:
    s = _schedule()
    start, stop = firing_window(
        s.lead_min, s.run_min, NOW + timedelta(minutes=5)
    )  # window around now
    windows = [(s, start, stop)]
    idle = SchedulerState()
    # in window, idle, not capturing -> start
    assert next_decision(windows, NOW, idle, capturing=False) == ("start", s)
    # a manual capture owns the SDR -> none
    assert next_decision(windows, NOW, idle, capturing=True) == ("none", None)
    # already fired this window -> none
    s_fired = _schedule(last_run_at=start)
    assert next_decision([(s_fired, start, stop)], NOW, idle, False) == ("none", None)
    # running + past stop -> stop
    running = SchedulerState(running_schedule_id=1, stop_at=NOW - timedelta(seconds=1))
    assert next_decision(windows, NOW, running, False)[0] == "stop"
    # running + before stop -> none
    running2 = SchedulerState(running_schedule_id=1, stop_at=NOW + timedelta(minutes=10))
    assert next_decision(windows, NOW, running2, False) == ("none", None)


# --- scheduler_tick (async, IO mocked) -------------------------------------


class _Ctl:
    def __init__(self, capturing: bool = False) -> None:
        self.calls: list[dict[str, Any]] = []
        self.capturing = capturing

    def __call__(self, endpoint: str, request: dict[str, Any], **kw: Any) -> dict[str, Any]:
        self.calls.append(request)
        if request["cmd"] == "status":
            return {"ok": True, "capturing": self.capturing}
        return {"ok": True, "capturing": request["cmd"] == "start_capture"}


def _app_with_schedule(tmp_path, repeat: str = "daily") -> tuple[Any, Engine, int]:
    engine = init_db(tmp_path)
    app = create_app(Settings(zmq_endpoint=DEAD_ENDPOINT, data_dir=str(tmp_path)), engine=engine)
    with Session(engine) as s:
        src = s.exec(select(RadioSource)).first()
        row = Schedule(
            name="t", source_id=src.id, lead_min=5, run_min=30, format="npz", repeat=repeat
        )
        s.add(row)
        s.commit()
        s.refresh(row)
        sid = row.id
    return app, engine, sid


def _window_now(sid: int, engine: Engine, start_off: int, stop_off: int):
    """A monkeypatch for _compute_windows returning one window around NOW."""

    def fake(session, now):  # noqa: ANN001
        row = session.get(Schedule, sid)
        return [(row, now + timedelta(minutes=start_off), now + timedelta(minutes=stop_off))]

    return fake


def _big_disk(_p: Any) -> SimpleNamespace:
    return SimpleNamespace(free=900e9, total=1000e9, used=100e9)


def test_tick_starts_capture_when_window_open(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    app, engine, sid = _app_with_schedule(tmp_path)
    ctl = _Ctl(capturing=False)
    monkeypatch.setattr(sched, "ctl_request", ctl)
    monkeypatch.setattr(sched, "_compute_windows", _window_now(sid, engine, -1, 29))
    monkeypatch.setattr(sched.shutil, "disk_usage", _big_disk)

    asyncio.run(sched.scheduler_tick(app, NOW))
    assert {"cmd": "start_capture", "format": "npz"} in ctl.calls
    assert app.state.scheduler.running_schedule_id == sid
    with Session(engine) as s:
        assert s.get(Schedule, sid).last_run_at is not None  # window consumed


def test_tick_refuses_when_disk_would_go_red(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    app, engine, sid = _app_with_schedule(tmp_path)
    ctl = _Ctl(capturing=False)
    monkeypatch.setattr(sched, "ctl_request", ctl)
    monkeypatch.setattr(sched, "_compute_windows", _window_now(sid, engine, -1, 29))
    monkeypatch.setattr(
        sched.shutil, "disk_usage", lambda _p: SimpleNamespace(free=1e9, total=1000e9, used=999e9)
    )

    asyncio.run(sched.scheduler_tick(app, NOW))
    assert not any(c["cmd"] == "start_capture" for c in ctl.calls)  # refused
    assert app.state.scheduler.running_schedule_id is None
    assert "refused" in app.state.scheduler.notes.get(sid, "")


def test_tick_skips_when_a_capture_is_running(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    app, engine, sid = _app_with_schedule(tmp_path)
    ctl = _Ctl(capturing=True)  # something already owns the SDR
    monkeypatch.setattr(sched, "ctl_request", ctl)
    monkeypatch.setattr(sched, "_compute_windows", _window_now(sid, engine, -1, 29))
    monkeypatch.setattr(sched.shutil, "disk_usage", _big_disk)

    asyncio.run(sched.scheduler_tick(app, NOW))
    assert not any(c["cmd"] == "start_capture" for c in ctl.calls)
    assert app.state.scheduler.running_schedule_id is None


def test_tick_stops_at_window_end_and_disables_once(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, engine, sid = _app_with_schedule(tmp_path, repeat="once")
    ctl = _Ctl(capturing=True)
    monkeypatch.setattr(sched, "ctl_request", ctl)
    monkeypatch.setattr(sched, "_compute_windows", _window_now(sid, engine, -40, -10))
    monkeypatch.setattr(sched.shutil, "disk_usage", _big_disk)
    registered: list[Any] = []
    monkeypatch.setattr(
        "jansky_observe.server.routers.captures.register_stopped_capture",
        lambda engine, reply: registered.append(reply),
    )
    app.state.scheduler = SchedulerState(
        running_schedule_id=sid, stop_at=NOW - timedelta(seconds=1)
    )

    asyncio.run(sched.scheduler_tick(app, NOW))
    assert {"cmd": "stop_capture"} in ctl.calls
    assert registered  # the capture was registered
    assert app.state.scheduler.running_schedule_id is None
    with Session(engine) as s:
        assert s.get(Schedule, sid).enabled is False  # once -> disabled


# --- router ----------------------------------------------------------------


@pytest.fixture()
def client(tmp_path) -> TestClient:
    engine = init_db(tmp_path)
    return TestClient(
        create_app(Settings(zmq_endpoint=DEAD_ENDPOINT, data_dir=str(tmp_path)), engine=engine)
    )


def _source_id(client: TestClient) -> int:
    return client.get("/api/sources").json()[0]["id"]


def test_schedule_crud(client: TestClient) -> None:
    assert client.get("/api/schedules").json() == []
    sid = _source_id(client)
    resp = client.post(
        "/schedules",
        data={
            "name": "nightly Cyg",
            "source_id": sid,
            "lead_min": "5",
            "run_min": "30",
            "format": "npz",
            "repeat": "daily",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    rows = client.get("/api/schedules").json()
    assert len(rows) == 1 and rows[0]["name"] == "nightly Cyg" and rows[0]["enabled"] is True
    assert rows[0]["next_start"] is not None  # transit-derived window computed
    scid = rows[0]["id"]

    client.post(f"/schedules/{scid}/toggle", follow_redirects=False)
    assert client.get("/api/schedules").json()[0]["enabled"] is False

    client.post(f"/schedules/{scid}/delete", follow_redirects=False)
    assert client.get("/api/schedules").json() == []


def test_schedule_validation(client: TestClient) -> None:
    sid = _source_id(client)
    assert (
        client.post("/schedules", data={"name": "x", "source_id": sid, "format": "bad"}).status_code
        == 422
    )
    assert (
        client.post(
            "/schedules", data={"name": "x", "source_id": sid, "repeat": "weekly"}
        ).status_code
        == 422
    )
    assert (
        client.post("/schedules", data={"name": "x", "source_id": sid, "run_min": "0"}).status_code
        == 422
    )
    assert client.post("/schedules", data={"name": "x", "source_id": "9999"}).status_code == 404


def test_scheduler_status_and_page(client: TestClient) -> None:
    status = client.get("/api/scheduler_status").json()
    assert status["running_schedule_id"] is None
    assert "Scheduled captures" in client.get("/schedules").text


def test_compute_windows_real_astropy(tmp_path) -> None:
    """_compute_windows derives a 30-min window from a source's real transit."""
    engine = init_db(tmp_path)
    with Session(engine) as s:
        src = s.exec(select(RadioSource).where(RadioSource.name == "Cyg A")).first()
        assert src is not None
        s.add(Schedule(name="w", source_id=src.id, lead_min=5, run_min=30, enabled=True))
        s.add(Schedule(name="off", source_id=src.id, enabled=False))  # excluded
        s.commit()
        windows = sched._compute_windows(s, NOW)
    assert len(windows) == 1  # only the enabled one
    schedule, start, stop = windows[0]
    assert schedule.name == "w"
    assert stop - start == timedelta(minutes=30)
    # start = transit - lead, so the transit is 5 min after the window start.
    assert start < stop
