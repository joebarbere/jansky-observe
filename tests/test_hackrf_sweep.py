"""Tests for the HackRF RFI-sweep slice: cmd builder (bias-power guard),
run_sweep via the fake binary, the CSV summary, and the /api/rfi_sweep
endpoint against a fake REP daemon — no hardware, no network."""

from __future__ import annotations

import contextlib
import inspect
import json
import sys
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import zmq
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlmodel import Session, select

from jansky_observe.capture import hackrf_sweep
from jansky_observe.capture.hackrf_sweep import (
    build_sweep_cmd,
    compare_sweeps,
    rfi_sweep_comparison,
    run_sweep,
    summarize_sweep,
)
from jansky_observe.config import Settings
from jansky_observe.db import init_db
from jansky_observe.models import Capture, Observation, ObservationType, RadioSource, utcnow
from jansky_observe.server.app import create_app

FAKE = [sys.executable, str(Path(__file__).parent / "fake_hackrf_sweep.py")]
DEAD_ENDPOINT = "tcp://127.0.0.1:1"

# The fake's deterministic layout for -f 100:110 -w 1000000 (10 bins):
# peak1 at bin 2 (center 102.5 MHz, -38 dB), peak2 at bin 5 (105.5 MHz, -45 dB),
# noise alternating -70/-69 dB by sweep parity (mean -69.5 over even sweeps).
LO_MHZ, HI_MHZ = 100, 110
PEAK1_HZ, PEAK2_HZ = 102.5e6, 105.5e6


# ---- command builder ----------------------------------------------------------------


def test_build_sweep_cmd_flags():
    cmd = build_sweep_cmd(1000, 2000, bin_width_hz=1_000_000, num_sweeps=20)
    assert cmd == ["hackrf_sweep", "-f", "1000:2000", "-w", "1000000", "-N", "20"]


def test_build_sweep_cmd_accepts_argv_prefix_binary():
    cmd = build_sweep_cmd(binary=FAKE)
    assert cmd[: len(FAKE)] == FAKE


def test_build_sweep_cmd_rejects_bad_ranges():
    with pytest.raises(ValueError):
        build_sweep_cmd(2000, 1000)  # inverted
    with pytest.raises(ValueError):
        build_sweep_cmd(1000, 1000)  # empty
    with pytest.raises(ValueError):
        build_sweep_cmd(1000, 9000)  # beyond the HackRF tuning range
    with pytest.raises(ValueError):
        build_sweep_cmd(bin_width_hz=0)
    with pytest.raises(ValueError):
        build_sweep_cmd(num_sweeps=0)


def test_build_sweep_cmd_never_emits_antenna_power():
    """The bias-tee rule: no ``-p`` flag, and no parameter that could add one."""
    cmd = build_sweep_cmd(1000, 2000, bin_width_hz=2445, num_sweeps=1)
    assert "-p" not in cmd
    params = inspect.signature(build_sweep_cmd).parameters
    assert not any(
        name == "p" or "bias" in name or "antenna" in name or "power" in name for name in params
    )
    # Structural guard: the module never writes the flag at all.
    assert '"-p"' not in inspect.getsource(hackrf_sweep)


# ---- run_sweep ----------------------------------------------------------------------


def test_run_sweep_writes_csv_capture(tmp_path):
    path = run_sweep(tmp_path, LO_MHZ, HI_MHZ, num_sweeps=4, binary=FAKE)
    assert path.parent == tmp_path / "captures"
    assert path.name.startswith("rfi-") and path.name.endswith("Z.csv")
    lines = path.read_text().splitlines()
    assert len(lines) == 8  # 4 sweeps × 2 rows (10 bins, 5 per row)
    first = [p.strip() for p in lines[0].split(",")]
    assert float(first[2]) == LO_MHZ * 1e6  # raw CSV persisted verbatim
    assert len(first) == 6 + 5


def test_run_sweep_missing_binary_names_apt_package(tmp_path):
    with pytest.raises(RuntimeError, match="'hackrf' apt package"):
        run_sweep(tmp_path, binary="/nonexistent/hackrf_sweep")
    assert list((tmp_path / "captures").glob("*.csv")) == []  # no partial file


def test_run_sweep_nonzero_exit_raises(tmp_path):
    # sh -c 'exit 3' ignores the sweep flags and fails.
    with pytest.raises(RuntimeError, match="code 3"):
        run_sweep(tmp_path, binary=["/bin/sh", "-c", "exit 3", "hackrf_sweep"])
    assert list((tmp_path / "captures").glob("*.csv")) == []


# ---- summarize_sweep ----------------------------------------------------------------


def test_summarize_picks_loudest_averaged_across_sweeps(tmp_path):
    path = run_sweep(tmp_path, LO_MHZ, HI_MHZ, num_sweeps=4, binary=FAKE)
    summary = summarize_sweep(path)
    assert summary["n_rows"] == 8
    assert summary["freq_range_hz"] == [LO_MHZ * 1e6, HI_MHZ * 1e6]
    loudest = summary["loudest"]
    assert len(loudest) == 5
    assert loudest[0] == {"freq_hz": PEAK1_HZ, "power_db": pytest.approx(-38.0)}
    assert loudest[1] == {"freq_hz": PEAK2_HZ, "power_db": pytest.approx(-45.0)}
    # Noise bins alternate -70/-69 per sweep: the mean proves cross-sweep averaging.
    assert loudest[2]["power_db"] == pytest.approx(-69.5)


def test_summarize_top_n_and_skips_malformed_rows(tmp_path):
    path = run_sweep(tmp_path, LO_MHZ, HI_MHZ, num_sweeps=2, binary=FAKE)
    with path.open("a") as out:
        out.write("garbage line\n2026-01-01, bad, row, with, non-numeric, fields, here\n")
    summary = summarize_sweep(path, top_n=2)
    assert summary["n_rows"] == 4  # the malformed rows don't count
    assert [round(top["power_db"], 1) for top in summary["loudest"]] == [-38.0, -45.0]


def test_summarize_empty_csv_raises(tmp_path):
    empty = tmp_path / "rfi-empty.csv"
    empty.write_text("")
    with pytest.raises(ValueError, match="no sweep rows"):
        summarize_sweep(empty)


# ---- compare_sweeps / rfi_sweep_comparison (roadmap M6) -----------------------------

# One sweep row over 4 bins of 1 MHz starting at 1419 MHz → centers 1419.5,
# 1420.5, 1421.5, 1422.5 MHz. hackrf_sweep CSV columns: date, time, hz_low,
# hz_high, width, n_samples, then one power per bin.
_HZ_LOW = 1_419_000_000.0
_WIDTH = 1_000_000.0


def _write_sweep(path, powers):
    row = f"2026-01-01, 00:00:00, {_HZ_LOW}, {_HZ_LOW + 4 * _WIDTH}, {_WIDTH}, 20"
    path.write_text(row + ", " + ", ".join(str(p) for p in powers) + "\n")
    return path


def test_compare_sweeps_flags_risen_bins(tmp_path):
    before = _write_sweep(tmp_path / "before.csv", [-70, -68, -71, -69])
    after = _write_sweep(tmp_path / "after.csv", [-70, -50, -71, -66])  # bin1 +18, bin3 +3
    out = compare_sweeps(before, after, rise_db=6.0)
    assert out["n_risen"] == 1  # only the +18 dB bin clears the 6 dB threshold
    risen = out["risen"][0]
    assert risen["freq_hz"] == pytest.approx(_HZ_LOW + 1.5 * _WIDTH)  # 1420.5 MHz bin
    assert risen["delta_db"] == pytest.approx(18.0)
    assert risen["before_db"] == pytest.approx(-68.0)
    assert risen["after_db"] == pytest.approx(-50.0)
    assert out["after_loudest"][0]["power_db"] == pytest.approx(-50.0)


def test_compare_sweeps_steady_environment(tmp_path):
    before = _write_sweep(tmp_path / "b.csv", [-70, -68, -71, -69])
    after = _write_sweep(tmp_path / "a.csv", [-71, -69, -70, -70])  # all within noise
    assert compare_sweeps(before, after)["n_risen"] == 0


def test_rfi_sweep_comparison_pairs_first_and_last(tmp_path):
    b = _write_sweep(tmp_path / "b.csv", [-70, -68, -71, -69])
    a = _write_sweep(tmp_path / "a.csv", [-70, -50, -71, -69])
    caps = [
        SimpleNamespace(id=1, format="hackrf_sweep_csv", path=str(b), purged_at=None),
        SimpleNamespace(id=2, format="npz_spectra", path="x", purged_at=None),  # ignored
        SimpleNamespace(id=3, format="hackrf_sweep_csv", path=str(a), purged_at=None),
    ]
    out = rfi_sweep_comparison(caps)
    assert out is not None
    assert out["before_id"] == 1 and out["after_id"] == 3
    assert out["n_risen"] == 1


def test_rfi_sweep_comparison_needs_two_sweeps(tmp_path):
    b = _write_sweep(tmp_path / "b.csv", [-70, -68, -71, -69])
    one = [SimpleNamespace(id=1, format="hackrf_sweep_csv", path=str(b), purged_at=None)]
    assert rfi_sweep_comparison(one) is None
    assert rfi_sweep_comparison([]) is None


def test_rfi_sweep_comparison_skips_purged_and_missing(tmp_path):
    b = _write_sweep(tmp_path / "b.csv", [-70, -68, -71, -69])
    a = _write_sweep(tmp_path / "a.csv", [-70, -50, -71, -69])
    # One of the pair is purged → fewer than two usable → None.
    purged = [
        SimpleNamespace(id=1, format="hackrf_sweep_csv", path=str(b), purged_at="2026-01-01"),
        SimpleNamespace(id=2, format="hackrf_sweep_csv", path=str(a), purged_at=None),
    ]
    assert rfi_sweep_comparison(purged) is None
    # Two present but a file is gone → compare raises → None (best-effort).
    gone = [
        SimpleNamespace(id=1, format="hackrf_sweep_csv", path=str(b), purged_at=None),
        SimpleNamespace(id=2, format="hackrf_sweep_csv", path="/nope/missing.csv", purged_at=None),
    ]
    assert rfi_sweep_comparison(gone) is None


# ---- /api/rfi_sweep endpoint (fake REP daemon, pattern from test_captures_api) ------


@contextlib.contextmanager
def _fake_daemon(handler: Callable[[dict[str, Any]], dict[str, Any]]) -> Iterator[str]:
    """A plain ZMQ REP socket answering control requests with ``handler(request)``."""
    ctx = zmq.Context()
    sock = ctx.socket(zmq.REP)
    sock.bind("tcp://127.0.0.1:*")
    endpoint = sock.getsockopt_string(zmq.LAST_ENDPOINT)
    stop = threading.Event()

    def serve() -> None:
        while not stop.is_set():
            if sock.poll(50, zmq.POLLIN):
                request = json.loads(sock.recv())
                sock.send(json.dumps(handler(request)).encode())

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield endpoint
    finally:
        stop.set()
        thread.join(timeout=2.0)
        sock.close(0)
        ctx.term()


def _sweep_reply(csv_path: Path) -> dict[str, Any]:
    """A canned daemon rfi_sweep success reply pointing at a real CSV."""
    return {
        "ok": True,
        "path": str(csv_path),
        "num_sweeps": 20,
        "n_rows": 40,
        "freq_range_hz": [1.0e9, 2.0e9],
        "loudest": [{"freq_hz": 1.176e9, "power_db": -38.0}],
    }


def _real_csv(tmp_path: Path) -> Path:
    return run_sweep(tmp_path, LO_MHZ, HI_MHZ, num_sweeps=2, binary=FAKE)


def _running_observation(engine: Engine) -> int:
    with Session(engine) as session:
        obs_type = session.exec(select(ObservationType)).first()
        source = session.exec(select(RadioSource)).first()
        assert obs_type is not None and source is not None
        assert obs_type.id is not None and source.id is not None
        observation = Observation(
            name="rfi sweep test",
            observation_type_id=obs_type.id,
            station_id=1,
            location_id=1,
            source_id=source.id,
            status="running",
            actual_start=utcnow(),
        )
        session.add(observation)
        session.commit()
        session.refresh(observation)
        assert observation.id is not None
        return observation.id


def test_rfi_sweep_endpoint_registers_capture(tmp_path):
    engine = init_db(tmp_path)
    csv_path = _real_csv(tmp_path)
    obs_id = _running_observation(engine)
    received: list[dict[str, Any]] = []

    def handler(request: dict[str, Any]) -> dict[str, Any]:
        received.append(request)
        return _sweep_reply(csv_path)

    with _fake_daemon(handler) as endpoint:
        settings = Settings(
            zmq_endpoint=DEAD_ENDPOINT, ctl_endpoint=endpoint, data_dir=str(tmp_path)
        )
        client = TestClient(create_app(settings, engine=engine))
        resp = client.post("/api/rfi_sweep", json={"freq_lo_mhz": 1000, "freq_hi_mhz": 2000})
    assert resp.status_code == 200
    body = resp.json()
    assert received == [{"cmd": "rfi_sweep", "freq_lo_mhz": 1000.0, "freq_hi_mhz": 2000.0}]
    assert body["loudest"][0]["freq_hz"] == 1.176e9
    capture_id = body["capture_id"]
    assert isinstance(capture_id, int)
    with Session(engine) as session:
        capture = session.get(Capture, capture_id)
        assert capture is not None
        assert capture.device == "hackrf"
        assert capture.format == "hackrf_sweep_csv"
        assert capture.path == str(csv_path)
        assert capture.size_bytes == csv_path.stat().st_size
        assert capture.observation_id == obs_id  # linked to the running observation
        assert capture.sdr_settings["num_sweeps"] == 20


def test_rfi_sweep_endpoint_defaults_without_body(tmp_path):
    engine = init_db(tmp_path)
    csv_path = _real_csv(tmp_path)
    received: list[dict[str, Any]] = []

    def handler(request: dict[str, Any]) -> dict[str, Any]:
        received.append(request)
        return _sweep_reply(csv_path)

    with _fake_daemon(handler) as endpoint:
        settings = Settings(
            zmq_endpoint=DEAD_ENDPOINT, ctl_endpoint=endpoint, data_dir=str(tmp_path)
        )
        client = TestClient(create_app(settings, engine=engine))
        resp = client.post("/api/rfi_sweep")
    assert resp.status_code == 200
    assert received == [{"cmd": "rfi_sweep", "freq_lo_mhz": 1000.0, "freq_hi_mhz": 2000.0}]
    with Session(engine) as session:
        capture = session.get(Capture, resp.json()["capture_id"])
        assert capture is not None and capture.observation_id is None  # nothing running


def test_rfi_sweep_endpoint_daemon_refusal_409(tmp_path):
    engine = init_db(tmp_path)
    refusal = {"ok": False, "error": "capture in progress (data/captures/x.npz) — stop it first"}
    with _fake_daemon(lambda req: refusal) as endpoint:
        settings = Settings(
            zmq_endpoint=DEAD_ENDPOINT, ctl_endpoint=endpoint, data_dir=str(tmp_path)
        )
        client = TestClient(create_app(settings, engine=engine))
        resp = client.post("/api/rfi_sweep")
    assert resp.status_code == 409
    assert "capture in progress" in resp.json()["detail"]
    with Session(engine) as session:
        assert session.exec(select(Capture)).all() == []  # nothing registered


def test_rfi_sweep_endpoint_daemon_unreachable_503(tmp_path, monkeypatch):
    # Shrink the endpoint's 90 s sweep timeout so the unreachable case is fast.
    monkeypatch.setattr("jansky_observe.server.routers.captures._SWEEP_CTL_TIMEOUT_MS", 300)
    engine = init_db(tmp_path)
    settings = Settings(
        zmq_endpoint=DEAD_ENDPOINT, ctl_endpoint=DEAD_ENDPOINT, data_dir=str(tmp_path)
    )
    client = TestClient(create_app(settings, engine=engine))
    resp = client.post("/api/rfi_sweep")
    assert resp.status_code == 503


def test_index_page_has_rfi_sweep_button(tmp_path):
    engine = init_db(tmp_path)
    settings = Settings(zmq_endpoint=DEAD_ENDPOINT, data_dir=str(tmp_path))
    client = TestClient(create_app(settings, engine=engine))
    body = client.get("/").text
    assert 'id="btn-rfi-sweep"' in body and "RFI sweep" in body
    assert 'id="rfi-result"' in body
