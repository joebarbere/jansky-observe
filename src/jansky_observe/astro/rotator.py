"""Az/el rotator clients for the KrakenRF Discovery Drive (roadmap M9).

The Discovery Drive exposes two text control surfaces; both are implemented here
as small native clients so the Pi needs no hamlib binary:

- :class:`RotctlTcpRotator` — the **rotctld NET protocol** over TCP (the Drive's
  native surface; what GPredict/SatDump speak). Stdlib ``socket`` only.
- :class:`EasyCommSerialRotator` — **EasyComm II** over USB-serial (hamlib model
  202, 19200 8N1), the wired fallback. Uses ``pyserial`` at runtime; the transport
  is injectable so tests need no real port.
- :class:`SimRotator` — an in-process simulator that models a finite slew rate,
  driving the tests and the ``"sim"`` station mode. M9 is synthetic-first until the
  Drive is on the roof.

All implement :class:`Rotator`: ``get_position`` / ``set_position`` / ``stop`` /
``close``. These clients are thin transports — az/el **limit enforcement** and the
**park** position live on the Station record and are applied by the caller
(:mod:`jansky_observe.server.rotator`), never here. :func:`within_limits` is the
shared limit check; :func:`make_rotator` builds the configured client.
"""

from __future__ import annotations

import socket
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

__all__ = [
    "EasyCommSerialRotator",
    "Rotator",
    "RotatorError",
    "RotatorPosition",
    "RotctlTcpRotator",
    "SimRotator",
    "make_rotator",
    "within_limits",
]


class RotatorError(RuntimeError):
    """A rotator transport or protocol failure (unreachable, bad reply, timeout)."""


@dataclass(frozen=True)
class RotatorPosition:
    """A rotator az/el readback, in degrees."""

    az_deg: float
    el_deg: float


@runtime_checkable
class Rotator(Protocol):
    """A minimal rotator transport. Implementations are thin; limits live above."""

    def get_position(self) -> RotatorPosition:
        """Return the current az/el readback (raises :class:`RotatorError`)."""
        ...

    def set_position(self, az_deg: float, el_deg: float) -> None:
        """Command a slew to az/el (raises :class:`RotatorError`)."""
        ...

    def stop(self) -> None:
        """Halt motion immediately (raises :class:`RotatorError`)."""
        ...

    def close(self) -> None:
        """Release any transport resources. Idempotent."""
        ...


def within_limits(
    az_deg: float,
    el_deg: float,
    *,
    az_min: float,
    az_max: float,
    el_min: float,
    el_max: float,
) -> bool:
    """Whether an az/el target is inside the configured slew envelope (inclusive)."""
    return az_min <= az_deg <= az_max and el_min <= el_deg <= el_max


# --------------------------------------------------------------------------- sim


class SimRotator:
    """An in-process rotator that slews toward its target at a finite rate.

    Deterministic under an injected ``clock`` (a monotonic seconds source), so
    tests can advance time explicitly. ``set_position`` sets the target; each
    :meth:`get_position` advances the current az/el toward it by
    ``slew_rate_deg_s`` × elapsed. No limit checks — that is the caller's job.
    """

    def __init__(
        self,
        *,
        az_deg: float = 0.0,
        el_deg: float = 90.0,
        slew_rate_deg_s: float = 5.0,
        clock: object | None = None,
    ) -> None:
        import time

        self._az = az_deg
        self._el = el_deg
        self._target = RotatorPosition(az_deg, el_deg)
        self._rate = slew_rate_deg_s
        self._clock = clock if callable(clock) else time.monotonic
        self._last = self._clock()

    def _advance(self) -> None:
        now = self._clock()
        step = max(0.0, (now - self._last)) * self._rate
        self._last = now
        self._az += _clamp_step(self._az, self._target.az_deg, step)
        self._el += _clamp_step(self._el, self._target.el_deg, step)

    def get_position(self) -> RotatorPosition:
        self._advance()
        return RotatorPosition(round(self._az, 4), round(self._el, 4))

    def set_position(self, az_deg: float, el_deg: float) -> None:
        self._advance()
        self._target = RotatorPosition(az_deg, el_deg)

    def stop(self) -> None:
        self._advance()
        self._target = RotatorPosition(self._az, self._el)

    def close(self) -> None:
        return None


def _clamp_step(current: float, target: float, step: float) -> float:
    """Signed delta toward ``target`` no larger than ``step``."""
    delta = target - current
    if abs(delta) <= step:
        return delta
    return step if delta > 0 else -step


# ---------------------------------------------------------------------- rotctl


class RotctlTcpRotator:
    """rotctld NET-protocol client over TCP (connect-per-command, so a dropped
    Drive never leaves a stale socket). Commands: ``P az el`` (set), ``p`` (get),
    ``S`` (stop); success is ``RPRT 0``.
    """

    def __init__(self, host: str, port: int = 4533, *, timeout: float = 5.0) -> None:
        self._host = host
        self._port = port
        self._timeout = timeout

    def _exchange(self, command: str, reply_lines: int) -> list[str]:
        # rotctld replies with a fixed number of lines per command (1 ``RPRT`` for
        # set/stop, 2 float lines for ``p``), so read exactly that many and stop —
        # waiting for anything more would block on a connection the server is now
        # holding open for the next command.
        try:
            with socket.create_connection((self._host, self._port), self._timeout) as sock:
                sock.settimeout(self._timeout)
                sock.sendall(command.encode("ascii"))
                fp = sock.makefile("r", encoding="ascii", newline="\n")
                lines: list[str] = []
                for _ in range(reply_lines):
                    line = fp.readline()
                    if not line:
                        break
                    lines.append(line.strip())
                return lines
        except (TimeoutError, OSError) as exc:
            raise RotatorError(f"rotctld at {self._host}:{self._port}: {exc}") from exc

    @staticmethod
    def _check_rprt(lines: list[str]) -> None:
        for line in lines:
            if line.upper().startswith("RPRT"):
                parts = line.split()
                code = int(parts[1]) if len(parts) > 1 else 0
                if code != 0:
                    raise RotatorError(f"rotctld returned RPRT {code}")
                return
        # No RPRT line at all — some builds stay silent on success; accept it.

    def get_position(self) -> RotatorPosition:
        lines = self._exchange("p\n", reply_lines=2)
        floats: list[float] = []
        for line in lines:
            if not line or line.upper().startswith("RPRT"):
                continue
            try:
                floats.append(float(line.split()[0]))
            except (ValueError, IndexError):
                continue
        if len(floats) < 2:
            raise RotatorError(f"rotctld gave no az/el (got {lines!r})")
        return RotatorPosition(floats[0], floats[1])

    def set_position(self, az_deg: float, el_deg: float) -> None:
        self._check_rprt(self._exchange(f"P {az_deg:.6f} {el_deg:.6f}\n", reply_lines=1))

    def stop(self) -> None:
        self._check_rprt(self._exchange("S\n", reply_lines=1))

    def close(self) -> None:
        return None


# -------------------------------------------------------------------- easycomm


@runtime_checkable
class _SerialTransport(Protocol):
    """The slice of ``pyserial.Serial`` the EasyComm client uses (injectable)."""

    def write(self, data: bytes) -> int | None: ...

    def readline(self) -> bytes: ...

    def close(self) -> None: ...


class EasyCommSerialRotator:
    """EasyComm II client over a serial transport (hamlib model 202, 19200 8N1).

    Commands: ``AZ<az> EL<el>`` (set), ``AZ EL`` (query → ``AZ<az> EL<el>``),
    ``SA SE`` (stop). The ``transport`` is any object with ``write``/``readline``/
    ``close`` — a real :class:`serial.Serial` in production, a fake in tests.
    """

    def __init__(self, transport: _SerialTransport) -> None:
        self._port = transport

    def _write(self, line: str) -> None:
        try:
            self._port.write(line.encode("ascii"))
        except OSError as exc:
            raise RotatorError(f"EasyComm write failed: {exc}") from exc

    def get_position(self) -> RotatorPosition:
        self._write("AZ EL\n")
        try:
            reply = self._port.readline().decode("ascii", "replace")
        except OSError as exc:
            raise RotatorError(f"EasyComm read failed: {exc}") from exc
        az = _easycomm_field(reply, "AZ")
        el = _easycomm_field(reply, "EL")
        if az is None or el is None:
            raise RotatorError(f"EasyComm gave no az/el (got {reply!r})")
        return RotatorPosition(az, el)

    def set_position(self, az_deg: float, el_deg: float) -> None:
        self._write(f"AZ{az_deg:.1f} EL{el_deg:.1f}\n")

    def stop(self) -> None:
        self._write("SA SE\n")

    def close(self) -> None:
        try:
            self._port.close()
        except OSError:
            pass


def _easycomm_field(reply: str, key: str) -> float | None:
    """Parse the float after an ``AZ``/``EL`` token in an EasyComm reply."""
    for token in reply.replace("\n", " ").split():
        if token.upper().startswith(key):
            try:
                return float(token[len(key) :])
            except ValueError:
                return None
    return None


# ---------------------------------------------------------------------- factory


def make_rotator(
    kind: str,
    *,
    host: str = "",
    port: int = 4533,
    serial_device: str = "",
    baud: int = 19200,
    timeout: float = 5.0,
) -> Rotator | None:
    """Build the configured rotator client, or ``None`` for ``kind == "none"``.

    Parameters mirror the Station rotator fields. ``"easycomm"`` opens the real
    serial port here (importing ``pyserial`` lazily), so it is only touched when a
    station is actually EasyComm-configured.

    Raises
    ------
    RotatorError
        Unknown ``kind``, or a serial port that will not open.
    """
    if kind == "none":
        return None
    if kind == "sim":
        return SimRotator()
    if kind == "rotctl":
        return RotctlTcpRotator(host, port, timeout=timeout)
    if kind == "easycomm":
        try:
            import serial  # pyserial; lazy so import-time never needs it
        except ImportError as exc:  # pragma: no cover - pyserial is a declared dep
            raise RotatorError("pyserial is required for EasyComm II") from exc
        try:
            transport = serial.Serial(serial_device, baud, timeout=timeout)
        except (OSError, ValueError) as exc:
            raise RotatorError(f"EasyComm serial {serial_device!r}: {exc}") from exc
        return EasyCommSerialRotator(transport)
    raise RotatorError(f"unknown rotator kind {kind!r}")
