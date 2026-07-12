"""Station diagnostics — the debug bundle a remote Claude needs for this class
of setup (roadmap M6, plan §12).

One function, :func:`collect_diagnostics`, gathers seven best-effort checks in a
fixed order — the same order ``/troubleshoot-chain`` walks its *software*
questions:

1. **systemd** — are the two units active/enabled?
2. **usb** — is each SDR (Airspy / HackRF) enumerated on the bus?
3. **daemon** — does the capture daemon answer its control channel, and how old
   is the last frame the relay saw (is data actually flowing)?
4. **thermals** — ``vcgencmd measure_temp`` + the decoded ``get_throttled``
   bitmask (undervoltage / soft-temp-limit flags — this Pi has brushed the
   limit).
5. **disk** — free/total on the data volume, with the amber-<20 % / red-<10 %
   thresholds the status-bar gauge uses.
6. **database** — ``PRAGMA user_version`` against the latest migration.
7. **journal** — the last error lines from each unit's journal.

Every check is *best-effort and independently degradable*: a station without
``vcgencmd`` (a laptop) or ``systemctl`` (a container) reports those checks as
``"unavailable"`` with a reason, never raises, and never blocks the others. All
of the syscalls are small ``subprocess`` shells, so the caller runs this off the
event loop (``asyncio.to_thread``).

Each check returns a dict carrying a ``status`` in
``{"ok", "warn", "error", "unavailable"}`` plus check-specific detail.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from typing import TYPE_CHECKING, Any

from jansky_observe import __version__
from jansky_observe.control import ctl_request
from jansky_observe.db import MIGRATIONS

if TYPE_CHECKING:  # pragma: no cover
    from sqlalchemy import Engine

    from jansky_observe.server.app import Broadcaster

__all__ = ["collect_diagnostics"]

SERVER_UNIT = "jansky-observe.service"
CAPTURE_UNIT = "jansky-observe-capture.service"

# vendor:product of the station's receivers — the same ids the udev rules match
# (deploy/udev/99-jansky-observe-sdr.rules).
_SDR_USB_IDS = {
    "airspy": ("1d50", "60a1"),
    "hackrf": ("1d50", "6089"),
}

# Raspberry Pi ``vcgencmd get_throttled`` bit meanings.
_THROTTLE_BITS = {
    0: "under-voltage detected",
    1: "arm frequency capped",
    2: "currently throttled",
    3: "soft temperature limit active",
    16: "under-voltage has occurred",
    17: "arm frequency capping has occurred",
    18: "throttling has occurred",
    19: "soft temperature limit has occurred",
}
# Bits 0–3 are live conditions (a problem right now); 16–19 are sticky
# "has occurred since boot" history.
_THROTTLE_ACTIVE_MASK = 0b1111

_CTL_TIMEOUT_MS = 1500
_SUBPROCESS_TIMEOUT_S = 4.0
_STALE_FRAME_S = 10.0
_DISK_AMBER_FRACTION = 0.20
_DISK_RED_FRACTION = 0.10
_GB = 1e9


def _run(
    *cmd: str, timeout: float = _SUBPROCESS_TIMEOUT_S
) -> subprocess.CompletedProcess[str] | None:
    """Run a command, returning the completed process, or ``None`` if the
    binary is absent or the call times out. Never raises."""
    try:
        return subprocess.run(  # noqa: S603 — fixed argv, no shell, no user input
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None


def _systemd_check() -> dict[str, Any]:
    """systemd unit states for the server + capture units (check 1)."""
    probe = _run("systemctl", "--version")
    if probe is None:
        return {"status": "unavailable", "reason": "systemctl not found (not a systemd host)"}
    units: dict[str, dict[str, str]] = {}
    any_active = False
    for unit in (SERVER_UNIT, CAPTURE_UNIT):
        active = _run("systemctl", "is-active", unit)
        enabled = _run("systemctl", "is-enabled", unit)
        active_state = active.stdout.strip() if active else "unknown"
        units[unit] = {
            "active": active_state,
            "enabled": enabled.stdout.strip() if enabled else "unknown",
        }
        any_active = any_active or active_state == "active"
    # Both units active → ok; otherwise flag it (a stopped capture daemon is the
    # usual "no data" cause). "error" only when neither is up.
    all_active = all(u["active"] == "active" for u in units.values())
    status = "ok" if all_active else ("warn" if any_active else "error")
    return {"status": status, "units": units}


def _usb_check() -> dict[str, Any]:
    """SDR USB enumeration — is each receiver on the bus? (check 2)."""
    result = _run("lsusb")
    if result is None or result.returncode != 0:
        return {"status": "unavailable", "reason": "lsusb not available"}
    listing = result.stdout.lower()
    devices = {name: f"{vid}:{pid}" in listing for name, (vid, pid) in _SDR_USB_IDS.items()}
    # Informational, not an error: on a laptop or a bench without the SDR
    # plugged in this is expected. The status reflects "did we see anything".
    status = "ok" if any(devices.values()) else "warn"
    return {"status": status, "devices": devices}


def _daemon_check(
    ctl_endpoint: str, broadcaster: Broadcaster, *, now: float | None = None
) -> dict[str, Any]:
    """Capture-daemon control reply + age of the last frame the relay saw (check 3)."""
    reply = ctl_request(ctl_endpoint, {"cmd": "status"}, timeout_ms=_CTL_TIMEOUT_MS)
    now = time.time() if now is None else now
    latest = broadcaster.latest
    frame_age_s = None if latest is None else max(0.0, now - latest.timestamp)
    out: dict[str, Any] = {
        "reachable": bool(reply.get("ok")),
        "source": reply.get("source"),
        "capturing": reply.get("capturing"),
        "fps": broadcaster.fps,
        "frame_age_s": frame_age_s,
    }
    if not reply.get("ok"):
        out["status"] = "error"
        out["error"] = reply.get("error")
    elif frame_age_s is None:
        # Daemon answers but the relay has never seen a frame — data is not flowing.
        out["status"] = "warn"
    elif frame_age_s > _STALE_FRAME_S:
        out["status"] = "warn"
        out["stale"] = True
    else:
        out["status"] = "ok"
    return out


def _parse_temp(text: str) -> float | None:
    """Parse ``temp=47.2'C`` from ``vcgencmd measure_temp``."""
    try:
        return float(text.split("=", 1)[1].split("'", 1)[0])
    except (IndexError, ValueError):
        return None


def _decode_throttled(text: str) -> dict[str, Any] | None:
    """Decode ``throttled=0x50005`` into the set live/sticky flags."""
    try:
        value = int(text.split("=", 1)[1].strip(), 16)
    except (IndexError, ValueError):
        return None
    flags = [label for bit, label in _THROTTLE_BITS.items() if value & (1 << bit)]
    return {
        "raw": hex(value),
        "flags": flags,
        "under_load_now": bool(value & _THROTTLE_ACTIVE_MASK),
    }


def _thermals_check() -> dict[str, Any]:
    """Pi SoC temperature + throttle flags (check 4)."""
    temp = _run("vcgencmd", "measure_temp")
    if temp is None:
        return {"status": "unavailable", "reason": "vcgencmd not found (not a Raspberry Pi)"}
    temp_c = _parse_temp(temp.stdout.strip())
    throttled_raw = _run("vcgencmd", "get_throttled")
    throttled = _decode_throttled(throttled_raw.stdout.strip()) if throttled_raw else None
    out: dict[str, Any] = {"temp_c": temp_c, "throttled": throttled}
    # Live throttling is an error; a sticky "has occurred" flag is a warning.
    if throttled and throttled["under_load_now"]:
        out["status"] = "error"
    elif throttled and throttled["flags"]:
        out["status"] = "warn"
    else:
        out["status"] = "ok"
    return out


def _disk_check(data_dir: str) -> dict[str, Any]:
    """Free/total on the data volume with the status-bar thresholds (check 5)."""
    try:
        usage = shutil.disk_usage(data_dir)
    except OSError as exc:
        return {"status": "unavailable", "reason": str(exc)}
    fraction_free = usage.free / usage.total if usage.total else 0.0
    if fraction_free < _DISK_RED_FRACTION:
        status = "error"
    elif fraction_free < _DISK_AMBER_FRACTION:
        status = "warn"
    else:
        status = "ok"
    smart = _smart_summary(data_dir)
    return {
        "status": status,
        "free_gb": round(usage.free / _GB, 1),
        "total_gb": round(usage.total / _GB, 1),
        "fraction_free": round(fraction_free, 3),
        "smart": smart,
    }


def _smart_summary(data_dir: str) -> dict[str, Any]:
    """Best-effort SMART health for the data volume (NVMe yes, SD card no)."""
    which = _run("df", "--output=source", data_dir)
    if which is None or which.returncode != 0:
        return {"available": False, "reason": "cannot resolve the data device"}
    lines = [ln.strip() for ln in which.stdout.splitlines() if ln.strip()]
    device = lines[-1] if len(lines) >= 2 else None
    if device is None:
        return {"available": False, "reason": "cannot resolve the data device"}
    health = _run("smartctl", "-H", device)
    if health is None:
        return {"available": False, "device": device, "reason": "smartctl not installed"}
    passed = "PASSED" in health.stdout or "OK" in health.stdout
    return {"available": True, "device": device, "healthy": passed}


def _database_check(engine: Engine | None) -> dict[str, Any]:
    """``PRAGMA user_version`` vs the latest migration (check 6)."""
    expected = max(version for version, _ in MIGRATIONS)
    if engine is None:
        return {"status": "unavailable", "reason": "no database engine", "expected": expected}
    try:
        with engine.connect() as conn:
            current = int(conn.exec_driver_sql("PRAGMA user_version").scalar_one())
    except Exception as exc:  # noqa: BLE001 — best-effort probe, never propagate
        return {"status": "error", "reason": str(exc), "expected": expected}
    return {
        "status": "ok" if current == expected else "warn",
        "user_version": current,
        "expected": expected,
    }


def _journal_check(lines: int = 10) -> dict[str, Any]:
    """Last error-priority journal lines from each unit (check 7)."""
    probe = _run("journalctl", "--version")
    if probe is None:
        return {"status": "unavailable", "reason": "journalctl not found"}
    units: dict[str, list[str]] = {}
    for unit in (SERVER_UNIT, CAPTURE_UNIT):
        result = _run("journalctl", "-u", unit, "-p", "err", "-n", str(lines), "--no-pager", "-q")
        if result is None or result.returncode != 0:
            units[unit] = []
            continue
        units[unit] = [ln for ln in result.stdout.splitlines() if ln.strip()]
    any_errors = any(units.values())
    return {"status": "warn" if any_errors else "ok", "recent_errors": units}


def collect_diagnostics(
    *,
    data_dir: str,
    ctl_endpoint: str,
    broadcaster: Broadcaster,
    engine: Engine | None,
    now: float | None = None,
) -> dict[str, Any]:
    """Gather the full station diagnostics bundle (roadmap M6).

    Parameters
    ----------
    data_dir : str
        The station data directory (``Settings.data_dir``) — the disk gauge and
        SMART probe target this volume.
    ctl_endpoint : str
        The capture daemon's control endpoint (``Settings.ctl_endpoint``).
    broadcaster : Broadcaster
        The live-frame fan-out, for the frame age and fps.
    engine : Engine or None
        The database engine (``None`` before the lifespan opens it).
    now : float, optional
        Wall-clock override (unix seconds) for the frame-age calculation; the
        current time by default. Used by tests.

    Returns
    -------
    dict
        ``{"version", "checks": {...}}`` with the seven checks in the fixed
        troubleshooting order. Each check has a ``status`` and its own detail;
        no check ever raises.
    """
    return {
        "version": __version__,
        "checks": {
            "systemd": _systemd_check(),
            "usb": _usb_check(),
            "daemon": _daemon_check(ctl_endpoint, broadcaster, now=now),
            "thermals": _thermals_check(),
            "disk": _disk_check(data_dir),
            "database": _database_check(engine),
            "journal": _journal_check(),
        },
    }
