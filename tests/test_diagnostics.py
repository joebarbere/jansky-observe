"""Tests for the station diagnostics bundle (roadmap M6).

Every check is best-effort, so the syscalls (``subprocess.run``,
``shutil.disk_usage``) are monkeypatched to make each branch deterministic —
no systemd/Pi/SDR required.
"""

from __future__ import annotations

import subprocess
from types import SimpleNamespace
from typing import Any

import pytest

from jansky_observe.db import MIGRATIONS, init_db
from jansky_observe.frames import SpectralFrame
from jansky_observe.server import diagnostics as diag


def _completed(stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=["x"], returncode=returncode, stdout=stdout, stderr="")


def _fake_run(table: dict[str, subprocess.CompletedProcess[str] | None]):
    """Build a ``subprocess.run`` stand-in dispatching on the first two argv
    tokens (e.g. ``"systemctl is-active"``), falling back to the binary name.
    A ``None`` value raises ``FileNotFoundError`` (binary absent)."""

    def run(cmd, **_kw):  # type: ignore[no-untyped-def]
        key2 = " ".join(cmd[:2])
        key1 = cmd[0]
        entry = table.get(key2, table.get(key1, _MISSING))
        if entry is _MISSING:
            return _completed("", returncode=0)
        if entry is None:
            raise FileNotFoundError(cmd[0])
        return entry

    return run


_MISSING = object()


class _FakeBroadcaster:
    def __init__(self, latest: SpectralFrame | None = None, fps: float | None = None) -> None:
        self.latest = latest
        self.fps = fps


def _frame(timestamp: float) -> SpectralFrame:
    import numpy as np

    return SpectralFrame(
        seq=1,
        timestamp=timestamp,
        center_freq_hz=1.4204e9,
        sample_rate_hz=3e6,
        power_db=np.zeros(64, dtype=np.float32),
    )


# --- pure parsers ----------------------------------------------------------


def test_parse_temp() -> None:
    assert diag._parse_temp("temp=47.2'C") == pytest.approx(47.2)
    assert diag._parse_temp("garbage") is None


def test_decode_throttled_live_and_sticky() -> None:
    # 0x50005 = bits 0,2 (live under-volt + throttled) and 16,18 (sticky).
    decoded = diag._decode_throttled("throttled=0x50005")
    assert decoded is not None
    assert decoded["under_load_now"] is True
    assert "under-voltage detected" in decoded["flags"]
    assert "throttling has occurred" in decoded["flags"]

    clean = diag._decode_throttled("throttled=0x0")
    assert clean == {"raw": "0x0", "flags": [], "under_load_now": False}

    assert diag._decode_throttled("nope") is None


# --- systemd ---------------------------------------------------------------


def test_systemd_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(diag.subprocess, "run", _fake_run({"systemctl": None}))
    out = diag._systemd_check()
    assert out["status"] == "unavailable"


def test_systemd_all_active(monkeypatch: pytest.MonkeyPatch) -> None:
    table = {
        "systemctl --version": _completed("systemd 257"),
        "systemctl is-active": _completed("active"),
        "systemctl is-enabled": _completed("enabled"),
    }
    monkeypatch.setattr(diag.subprocess, "run", _fake_run(table))
    out = diag._systemd_check()
    assert out["status"] == "ok"
    assert out["units"][diag.SERVER_UNIT]["active"] == "active"


def test_systemd_none_active_is_error(monkeypatch: pytest.MonkeyPatch) -> None:
    table = {
        "systemctl --version": _completed("systemd 257"),
        "systemctl is-active": _completed("inactive", returncode=3),
        "systemctl is-enabled": _completed("enabled"),
    }
    monkeypatch.setattr(diag.subprocess, "run", _fake_run(table))
    assert diag._systemd_check()["status"] == "error"


# --- usb -------------------------------------------------------------------


def test_usb_present(monkeypatch: pytest.MonkeyPatch) -> None:
    listing = "Bus 001 Device 004: ID 1d50:60a1 OpenMoko Airspy\n"
    monkeypatch.setattr(diag.subprocess, "run", _fake_run({"lsusb": _completed(listing)}))
    out = diag._usb_check()
    assert out["status"] == "ok"
    assert out["devices"]["airspy"] is True
    assert out["devices"]["hackrf"] is False


def test_usb_none_is_warn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(diag.subprocess, "run", _fake_run({"lsusb": _completed("nothing")}))
    assert diag._usb_check()["status"] == "warn"


def test_usb_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(diag.subprocess, "run", _fake_run({"lsusb": None}))
    assert diag._usb_check()["status"] == "unavailable"


# --- daemon ----------------------------------------------------------------


def test_daemon_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        diag, "ctl_request", lambda *a, **k: {"ok": False, "error": "did not reply"}
    )
    out = diag._daemon_check("tcp://x", _FakeBroadcaster(), now=1000.0)
    assert out["status"] == "error"
    assert out["reachable"] is False


def test_daemon_ok_fresh_frame(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        diag, "ctl_request", lambda *a, **k: {"ok": True, "source": "airspy", "capturing": False}
    )
    bc = _FakeBroadcaster(latest=_frame(999.0), fps=4.0)
    out = diag._daemon_check("tcp://x", bc, now=1000.0)
    assert out["status"] == "ok"
    assert out["source"] == "airspy"
    assert out["frame_age_s"] == pytest.approx(1.0)


def test_daemon_stale_frame(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(diag, "ctl_request", lambda *a, **k: {"ok": True, "source": "airspy"})
    bc = _FakeBroadcaster(latest=_frame(900.0), fps=4.0)
    out = diag._daemon_check("tcp://x", bc, now=1000.0)
    assert out["status"] == "warn"
    assert out["stale"] is True


def test_daemon_no_frame_yet(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(diag, "ctl_request", lambda *a, **k: {"ok": True, "source": "synthetic"})
    out = diag._daemon_check("tcp://x", _FakeBroadcaster(), now=1000.0)
    assert out["status"] == "warn"
    assert out["frame_age_s"] is None


# --- thermals --------------------------------------------------------------


def test_thermals_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(diag.subprocess, "run", _fake_run({"vcgencmd": None}))
    assert diag._thermals_check()["status"] == "unavailable"


def test_thermals_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    table = {
        "vcgencmd measure_temp": _completed("temp=45.1'C"),
        "vcgencmd get_throttled": _completed("throttled=0x0"),
    }
    monkeypatch.setattr(diag.subprocess, "run", _fake_run(table))
    out = diag._thermals_check()
    assert out["status"] == "ok"
    assert out["temp_c"] == pytest.approx(45.1)


def test_thermals_live_throttle_is_error(monkeypatch: pytest.MonkeyPatch) -> None:
    table = {
        "vcgencmd measure_temp": _completed("temp=85.0'C"),
        "vcgencmd get_throttled": _completed("throttled=0x8"),  # soft temp limit active
    }
    monkeypatch.setattr(diag.subprocess, "run", _fake_run(table))
    assert diag._thermals_check()["status"] == "error"


def test_thermals_sticky_flag_is_warn(monkeypatch: pytest.MonkeyPatch) -> None:
    table = {
        "vcgencmd measure_temp": _completed("temp=60.0'C"),
        "vcgencmd get_throttled": _completed("throttled=0x80000"),  # soft-limit occurred
    }
    monkeypatch.setattr(diag.subprocess, "run", _fake_run(table))
    assert diag._thermals_check()["status"] == "warn"


# --- disk ------------------------------------------------------------------


def _usage(free: float, total: float) -> SimpleNamespace:
    return SimpleNamespace(free=free, total=total, used=total - free)


def test_disk_thresholds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(diag, "_smart_summary", lambda _d: {"available": False})
    monkeypatch.setattr(
        diag.shutil, "disk_usage", lambda _p: _usage(500 * diag._GB, 1000 * diag._GB)
    )
    assert diag._disk_check("/data")["status"] == "ok"
    monkeypatch.setattr(
        diag.shutil, "disk_usage", lambda _p: _usage(150 * diag._GB, 1000 * diag._GB)
    )
    assert diag._disk_check("/data")["status"] == "warn"
    monkeypatch.setattr(
        diag.shutil, "disk_usage", lambda _p: _usage(50 * diag._GB, 1000 * diag._GB)
    )
    out = diag._disk_check("/data")
    assert out["status"] == "error"
    assert out["free_gb"] == pytest.approx(50.0)


def test_disk_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_p: Any) -> Any:
        raise OSError("gone")

    monkeypatch.setattr(diag.shutil, "disk_usage", boom)
    assert diag._disk_check("/nope")["status"] == "unavailable"


def test_smart_summary_nvme(monkeypatch: pytest.MonkeyPatch) -> None:
    table = {
        "df --output=source": _completed("Filesystem\n/dev/nvme0n1p2\n"),
        "smartctl -H": _completed("SMART overall-health self-assessment test result: PASSED"),
    }
    monkeypatch.setattr(diag.subprocess, "run", _fake_run(table))
    out = diag._smart_summary("/data")
    assert out["available"] is True
    assert out["healthy"] is True
    assert out["device"] == "/dev/nvme0n1p2"


def test_smart_summary_no_smartctl(monkeypatch: pytest.MonkeyPatch) -> None:
    table = {
        "df --output=source": _completed("Filesystem\n/dev/mmcblk0p2\n"),
        "smartctl -H": None,
    }
    monkeypatch.setattr(diag.subprocess, "run", _fake_run(table))
    out = diag._smart_summary("/data")
    assert out["available"] is False
    assert out["device"] == "/dev/mmcblk0p2"


# --- database --------------------------------------------------------------


def test_database_none() -> None:
    out = diag._database_check(None)
    assert out["status"] == "unavailable"
    assert out["expected"] == max(v for v, _ in MIGRATIONS)


def test_database_matches(tmp_path) -> None:
    engine = init_db(tmp_path)
    out = diag._database_check(engine)
    assert out["status"] == "ok"
    assert out["user_version"] == out["expected"]


# --- journal ---------------------------------------------------------------


def test_journal_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(diag.subprocess, "run", _fake_run({"journalctl": None}))
    assert diag._journal_check()["status"] == "unavailable"


def test_journal_reports_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    table = {
        "journalctl --version": _completed("systemd 257"),
        "journalctl -u": _completed("Jul 12 00:00:00 pi jansky-observe[1]: boom\n"),
    }
    monkeypatch.setattr(diag.subprocess, "run", _fake_run(table))
    out = diag._journal_check()
    assert out["status"] == "warn"
    assert out["recent_errors"][diag.SERVER_UNIT]


# --- top-level bundle ------------------------------------------------------


def test_collect_diagnostics_shape(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    # Every syscall absent → the syscall-backed checks degrade to unavailable,
    # but the bundle is still well-formed and never raises.
    monkeypatch.setattr(
        diag.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError())
    )
    monkeypatch.setattr(diag, "ctl_request", lambda *a, **k: {"ok": False, "error": "no daemon"})
    engine = init_db(tmp_path)
    bundle = diag.collect_diagnostics(
        data_dir=str(tmp_path),
        ctl_endpoint="tcp://127.0.0.1:9",
        broadcaster=_FakeBroadcaster(),
        engine=engine,
        now=1000.0,
    )
    assert set(bundle["checks"]) == {
        "systemd",
        "usb",
        "daemon",
        "thermals",
        "disk",
        "database",
        "journal",
    }
    assert bundle["checks"]["database"]["status"] == "ok"  # real engine
    assert bundle["checks"]["daemon"]["status"] == "error"  # no daemon
    assert all("status" in c for c in bundle["checks"].values())
