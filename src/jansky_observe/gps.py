"""Dependency-free gpsd client: read one position fix over raw TCP + JSON (plan §4.5).

GPS is **optional hardware** — the default station has no dongle and nothing
here runs unless the "Use GPS fix" button is pressed. When a USB GPS dongle is
present, `gpsd <https://gpsd.gitlab.io/gpsd/>`_ serves newline-delimited JSON
on TCP 2947; this module speaks that wire protocol directly — send
``?WATCH={"enable":true,"json":true}``, read report lines until a TPV with a
2D-or-better fix — so neither the ``gps`` Python package nor ``pynmea2`` is
needed.
"""

from __future__ import annotations

import json
import socket
import time
from dataclasses import dataclass

__all__ = ["DEFAULT_HOST", "DEFAULT_PORT", "GpsFix", "GpsUnavailable", "read_fix"]

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 2947
_WATCH_COMMAND = b'?WATCH={"enable":true,"json":true}\n'
_MODE_2D = 2
_MODE_3D = 3
_RECV_BYTES = 4096


class GpsUnavailable(RuntimeError):
    """gpsd could not be reached, or produced no usable TPV fix in time."""


@dataclass(frozen=True)
class GpsFix:
    """One position fix decoded from a gpsd TPV report."""

    lat_deg: float
    lon_deg: float
    #: Altitude in metres; ``None`` for a 2D fix (mode 2 carries no altitude).
    elevation_m: float | None
    #: gpsd fix mode: 2 = 2D, 3 = 3D.
    mode: int
    #: The TPV ``time`` field (ISO 8601 UTC), ``""`` when gpsd omitted it.
    time_utc: str


def _tpv_fix(line: bytes) -> GpsFix | None:
    """Decode one gpsd report line, or ``None`` when it is not a usable TPV.

    Usable means ``class == "TPV"``, ``mode >= 2``, and both ``lat`` and
    ``lon`` present. Altitude (mode 3 only) prefers ``altMSL`` (mean sea
    level, matching ``Location.elevation_m`` semantics) over ``altHAE`` over
    the pre-3.20 ``alt`` field.
    """
    try:
        report = json.loads(line)
    except ValueError:
        return None
    if not isinstance(report, dict) or report.get("class") != "TPV":
        return None
    mode = report.get("mode", 0)
    lat, lon = report.get("lat"), report.get("lon")
    if not isinstance(mode, int) or mode < _MODE_2D or lat is None or lon is None:
        return None
    elevation: float | None = None
    if mode >= _MODE_3D:
        for key in ("altMSL", "altHAE", "alt"):
            if report.get(key) is not None:
                elevation = float(report[key])
                break
    return GpsFix(
        lat_deg=float(lat),
        lon_deg=float(lon),
        elevation_m=elevation,
        mode=mode,
        time_utc=str(report.get("time", "")),
    )


def read_fix(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, timeout_s: float = 5.0) -> GpsFix:
    """Read one position fix from a running gpsd.

    Connects to gpsd's TCP socket, enables JSON watching, and returns the
    first TPV report with a 2D-or-better fix. The whole exchange shares one
    deadline of ``timeout_s`` seconds.

    Parameters
    ----------
    host, port : str, int
        The gpsd socket (default ``127.0.0.1:2947``).
    timeout_s : float
        Overall deadline covering connect, watch, and the wait for a fix.

    Returns
    -------
    GpsFix
        The first usable fix (``elevation_m`` is ``None`` for a 2D fix).

    Raises
    ------
    GpsUnavailable
        Connection refused/timed out, gpsd closed the connection, or no
        usable TPV arrived before the deadline.
    """
    deadline = time.monotonic() + timeout_s
    no_fix = (
        f"no usable GPS fix from gpsd at {host}:{port} within {timeout_s:.1f} s "
        "(is the GPS dongle plugged in, and does it see satellites?)"
    )
    try:
        sock = socket.create_connection((host, port), timeout=timeout_s)
    except OSError as exc:
        raise GpsUnavailable(
            f"cannot reach gpsd at {host}:{port} ({exc}) — is gpsd running? "
            "GPS is optional hardware (plan §4.5)"
        ) from exc
    with sock:
        try:
            sock.sendall(_WATCH_COMMAND)
            buffer = b""
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise GpsUnavailable(no_fix)
                sock.settimeout(remaining)
                try:
                    chunk = sock.recv(_RECV_BYTES)
                except TimeoutError:
                    raise GpsUnavailable(no_fix) from None
                if not chunk:
                    raise GpsUnavailable(
                        f"gpsd at {host}:{port} closed the connection before a usable TPV fix"
                    )
                buffer += chunk
                while b"\n" in buffer:
                    line, _, buffer = buffer.partition(b"\n")
                    fix = _tpv_fix(line)
                    if fix is not None:
                        return fix
        except OSError as exc:
            raise GpsUnavailable(f"error talking to gpsd at {host}:{port}: {exc}") from exc
