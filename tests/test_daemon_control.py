"""End-to-end control-channel tests: daemon thread driven via ctl_request."""

from __future__ import annotations

import sys
import threading
from pathlib import Path

import numpy as np

from jansky_observe.capture.daemon import run
from jansky_observe.capture.sources import SyntheticHISource
from jansky_observe.control import ctl_request

FAKE_HACKRF = [sys.executable, str(Path(__file__).parent / "fake_hackrf_sweep.py")]


def _run_daemon(tmp_path, max_frames=400, hackrf_binary="hackrf_sweep"):
    """Start run() in a thread; return (thread, pub_endpoint, ctl_endpoint)."""
    bound: dict[str, str] = {}
    ready = threading.Event()

    def on_ctl(endpoint: str) -> None:
        bound["ctl"] = endpoint
        ready.set()

    thread = threading.Thread(
        target=run,
        args=(SyntheticHISource(seed=1), "tcp://127.0.0.1:*"),
        kwargs={
            "fps": 200.0,
            "n_fft": 256,
            "averages": 2,
            "max_frames": max_frames,
            "warmup_s": 0.0,
            "ctl_endpoint": "tcp://127.0.0.1:*",
            "data_dir": str(tmp_path),
            "source_name": "synthetic",
            "on_ctl_bound": on_ctl,
            "hackrf_binary": hackrf_binary,
        },
        daemon=True,
    )
    thread.start()
    assert ready.wait(5), "daemon never bound its control socket"
    return thread, bound["ctl"]


def test_status_start_stop_npz(tmp_path):
    thread, ctl = _run_daemon(tmp_path)
    status = ctl_request(ctl, {"cmd": "status"})
    assert status["ok"] and status["capturing"] is False
    assert status["source"] == "synthetic"
    assert status["disk_free_bytes"] > 0

    started = ctl_request(ctl, {"cmd": "start_capture", "format": "npz"})
    assert started["ok"] and started["capturing"] and started["format"] == "npz"
    assert started["rate_bytes_per_s"] == 256 * 4 * 200.0

    again = ctl_request(ctl, {"cmd": "start_capture", "format": "npz"})
    assert again["ok"] is False and "already capturing" in again["error"]

    stopped = ctl_request(ctl, {"cmd": "stop_capture"})
    assert stopped["ok"] and stopped["path"].endswith(".npz")
    data = np.load(stopped["path"])
    assert data["power_db"].shape[0] >= 1

    idle_stop = ctl_request(ctl, {"cmd": "stop_capture"})
    assert idle_stop["ok"] is False
    thread.join(timeout=10)


def test_sigmf_capture_while_frames_flow(tmp_path):
    thread, ctl = _run_daemon(tmp_path)
    started = ctl_request(ctl, {"cmd": "start_capture", "format": "sigmf"})
    assert started["ok"] and started["format"] == "sigmf"
    assert started["rate_bytes_per_s"] == 3e6 * 4

    # Let frames (and the tap) flow, then stop.
    ctl_request(ctl, {"cmd": "status"})
    stopped = ctl_request(ctl, {"cmd": "stop_capture"})
    assert stopped["ok"] and stopped["bytes_written"] > 0
    data_path = stopped["path"]
    assert data_path.endswith(".sigmf-data")
    raw = np.fromfile(data_path, dtype="<i2")
    assert raw.size > 0 and raw.size % 2 == 0
    assert stopped["overrun"] is False
    thread.join(timeout=10)


def test_malformed_and_unknown_commands(tmp_path):
    thread, ctl = _run_daemon(tmp_path, max_frames=200)
    bad = ctl_request(ctl, {"cmd": "reticulate"})
    assert bad["ok"] is False and "unknown command" in bad["error"]
    missing_fmt = ctl_request(ctl, {"cmd": "start_capture"})
    assert missing_fmt["ok"] is False
    thread.join(timeout=10)


def test_rfi_sweep_round_trip(tmp_path):
    """rfi_sweep runs the (fake) hackrf_sweep, persists the CSV, replies a summary."""
    thread, ctl = _run_daemon(tmp_path, max_frames=600, hackrf_binary=FAKE_HACKRF)
    reply = ctl_request(
        ctl, {"cmd": "rfi_sweep", "freq_lo_mhz": 100, "freq_hi_mhz": 110}, timeout_ms=15000
    )
    assert reply["ok"] is True
    path = Path(reply["path"])
    assert path.is_file() and path.parent == tmp_path / "captures"
    assert path.name.startswith("rfi-") and path.suffix == ".csv"
    assert reply["freq_range_hz"] == [100e6, 110e6]
    assert reply["num_sweeps"] == 20
    # The fake's deterministic peaks (10 bins: -38 dB at bin 2, -45 dB at bin 5).
    assert reply["loudest"][0] == {"freq_hz": 102.5e6, "power_db": -38.0}
    assert reply["loudest"][1]["freq_hz"] == 105.5e6
    assert reply["n_rows"] == 40  # 20 sweeps × 2 rows
    thread.join(timeout=10)


def test_rfi_sweep_refused_while_capturing(tmp_path):
    thread, ctl = _run_daemon(tmp_path, hackrf_binary=FAKE_HACKRF)
    started = ctl_request(ctl, {"cmd": "start_capture", "format": "npz"})
    assert started["ok"] is True
    refused = ctl_request(ctl, {"cmd": "rfi_sweep"})
    assert refused["ok"] is False and "capture in progress" in refused["error"]
    assert list((tmp_path / "captures").glob("rfi-*.csv")) == []  # nothing swept
    stopped = ctl_request(ctl, {"cmd": "stop_capture"})
    assert stopped["ok"] is True
    thread.join(timeout=10)


def test_rfi_sweep_rejects_garbage_frequencies(tmp_path):
    thread, ctl = _run_daemon(tmp_path, max_frames=200)
    bad = ctl_request(ctl, {"cmd": "rfi_sweep", "freq_lo_mhz": "wide", "freq_hi_mhz": "open"})
    assert bad["ok"] is False and "must be numbers" in bad["error"]
    inverted = ctl_request(ctl, {"cmd": "rfi_sweep", "freq_lo_mhz": 2000, "freq_hi_mhz": 1000})
    assert inverted["ok"] is False
    thread.join(timeout=10)


def test_ctl_request_times_out_cleanly():
    reply = ctl_request("tcp://127.0.0.1:1", {"cmd": "status"}, timeout_ms=300)
    assert reply["ok"] is False and "did not reply" in reply["error"]
