"""End-to-end control-channel tests: daemon thread driven via ctl_request."""

from __future__ import annotations

import threading

import numpy as np

from jansky_observe.capture.daemon import run
from jansky_observe.capture.sources import SyntheticHISource
from jansky_observe.control import ctl_request


def _run_daemon(tmp_path, max_frames=400):
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


def test_ctl_request_times_out_cleanly():
    reply = ctl_request("tcp://127.0.0.1:1", {"cmd": "status"}, timeout_ms=300)
    assert reply["ok"] is False and "did not reply" in reply["error"]
