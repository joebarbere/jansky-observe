"""Tests for the M9 rotator clients, the Station glue, and the limit check.

No hardware: the rotctld leg runs against a fake rotctld TCP server, the EasyComm
leg against a fake serial transport, and the simulator against an injected clock.
"""

from __future__ import annotations

import socketserver
import threading
from collections.abc import Iterator

import pytest

from jansky_observe.astro.rotator import (
    EasyCommSerialRotator,
    RotatorError,
    RotatorPosition,
    RotctlTcpRotator,
    SimRotator,
    make_rotator,
    within_limits,
)
from jansky_observe.models import Station
from jansky_observe.server.rotator import (
    park_position,
    rotator_from_station,
    station_allows,
)

# ---- within_limits ------------------------------------------------------------


@pytest.mark.parametrize(
    ("az", "el", "expected"),
    [
        (180.0, 45.0, True),
        (0.0, 0.0, True),  # inclusive lower bound
        (360.0, 90.0, True),  # inclusive upper bound
        (-1.0, 45.0, False),
        (180.0, 91.0, False),
    ],
)
def test_within_limits(az: float, el: float, expected: bool) -> None:
    assert within_limits(az, el, az_min=0, az_max=360, el_min=0, el_max=90) is expected


# ---- SimRotator ---------------------------------------------------------------


class _FakeClock:
    """A manually advanced monotonic clock."""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def test_sim_rotator_slews_toward_target_at_finite_rate() -> None:
    clock = _FakeClock()
    rot = SimRotator(az_deg=0.0, el_deg=90.0, slew_rate_deg_s=5.0, clock=clock)
    rot.set_position(50.0, 90.0)  # 50° away in az

    clock.advance(4.0)  # 4 s × 5 °/s = 20°
    assert rot.get_position() == RotatorPosition(20.0, 90.0)

    clock.advance(100.0)  # plenty — reaches and holds the target
    assert rot.get_position() == RotatorPosition(50.0, 90.0)


def test_sim_rotator_stop_freezes_in_place() -> None:
    clock = _FakeClock()
    rot = SimRotator(az_deg=0.0, el_deg=90.0, slew_rate_deg_s=10.0, clock=clock)
    rot.set_position(100.0, 90.0)
    clock.advance(1.0)  # 10° in
    rot.stop()
    clock.advance(100.0)
    assert rot.get_position() == RotatorPosition(10.0, 90.0)  # did not resume


# ---- rotctld TCP --------------------------------------------------------------


class _RotctldHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        for raw in self.rfile:
            cmd = raw.decode("ascii").strip()
            if cmd.startswith("P"):
                _, az, el = cmd.split()
                if float(az) < 0:  # simulate a hardware rejection
                    self.wfile.write(b"RPRT -1\n")
                else:
                    self.server.pos = (float(az), float(el))  # type: ignore[attr-defined]
                    self.wfile.write(b"RPRT 0\n")
            elif cmd == "p":
                az, el = self.server.pos  # type: ignore[attr-defined]
                self.wfile.write(f"{az:.6f}\n{el:.6f}\n".encode())
            elif cmd == "S":
                self.wfile.write(b"RPRT 0\n")
            self.wfile.flush()


@pytest.fixture()
def rotctld() -> Iterator[tuple[str, int]]:
    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _RotctldHandler)
    server.pos = (0.0, 90.0)  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[0], server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


def test_rotctl_set_get_stop_round_trip(rotctld: tuple[str, int]) -> None:
    host, port = rotctld
    rot = RotctlTcpRotator(host, port, timeout=2.0)
    rot.set_position(123.45, 45.6)
    assert rot.get_position() == RotatorPosition(123.45, 45.6)
    rot.stop()  # RPRT 0, no raise


def test_rotctl_raises_on_error_rprt(rotctld: tuple[str, int]) -> None:
    host, port = rotctld
    rot = RotctlTcpRotator(host, port, timeout=2.0)
    with pytest.raises(RotatorError, match="RPRT -1"):
        rot.set_position(-5.0, 45.0)  # the fake rejects az < 0


def test_rotctl_unreachable_raises() -> None:
    rot = RotctlTcpRotator("127.0.0.1", 1, timeout=0.5)  # nothing listening
    with pytest.raises(RotatorError):
        rot.get_position()


# ---- EasyComm II serial -------------------------------------------------------


class _FakeSerial:
    def __init__(self, reply: bytes = b"AZ123.4 EL045.6\n") -> None:
        self.writes: list[bytes] = []
        self._reply = reply
        self.closed = False

    def write(self, data: bytes) -> int:
        self.writes.append(data)
        return len(data)

    def readline(self) -> bytes:
        return self._reply

    def close(self) -> None:
        self.closed = True


def test_easycomm_set_get_stop() -> None:
    fake = _FakeSerial()
    rot = EasyCommSerialRotator(fake)
    rot.set_position(123.4, 45.6)
    assert fake.writes[-1] == b"AZ123.4 EL45.6\n"
    assert rot.get_position() == RotatorPosition(123.4, 45.6)
    rot.stop()
    assert fake.writes[-1] == b"SA SE\n"
    rot.close()
    assert fake.closed


def test_easycomm_bad_reply_raises() -> None:
    rot = EasyCommSerialRotator(_FakeSerial(reply=b"garbage\n"))
    with pytest.raises(RotatorError, match="no az/el"):
        rot.get_position()


# ---- make_rotator factory -----------------------------------------------------


def test_make_rotator_kinds(monkeypatch: pytest.MonkeyPatch) -> None:
    assert make_rotator("none") is None
    assert isinstance(make_rotator("sim"), SimRotator)
    assert isinstance(make_rotator("rotctl", host="localhost"), RotctlTcpRotator)

    import serial  # pyserial is a declared dep

    monkeypatch.setattr(serial, "Serial", lambda *a, **k: _FakeSerial())
    assert isinstance(make_rotator("easycomm", serial_device="/dev/ttyUSB0"), EasyCommSerialRotator)

    with pytest.raises(RotatorError, match="unknown rotator kind"):
        make_rotator("bogus")


# ---- Station glue -------------------------------------------------------------


def _station(**kw: object) -> Station:
    base = dict(name="S", dish_diameter_m=0.7, dish_f_d=0.38)
    base.update(kw)
    return Station(**base)  # type: ignore[arg-type]


def test_rotator_from_station() -> None:
    assert rotator_from_station(_station(rotator_kind="none")) is None
    assert isinstance(rotator_from_station(_station(rotator_kind="sim")), SimRotator)


def test_station_allows_respects_limits() -> None:
    station = _station(az_min_deg=10, az_max_deg=200, el_min_deg=5, el_max_deg=80)
    assert station_allows(station, 100.0, 45.0)
    assert not station_allows(station, 5.0, 45.0)  # below az_min
    assert not station_allows(station, 100.0, 85.0)  # above el_max


def test_park_position_defaults_straight_up() -> None:
    assert park_position(_station()) == RotatorPosition(0.0, 90.0)
