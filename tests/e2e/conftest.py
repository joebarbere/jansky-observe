"""Fixtures for the browser end-to-end suite (``tests/e2e``, marked ``e2e``).

The ``live_server`` fixture mirrors the ``deploy/install.sh`` ``--smoke`` path
and the ``/verify`` end-to-end smoke: it launches the synthetic capture daemon
and the API server as real subprocesses on free ports, wired to a throwaway
data dir, waits for ``/healthz``, and yields the server's base URL. Both
processes are torn down at the end of the session.

These tests need chromium and a running server, so they are excluded from the
default unit run (``addopts = "-ra -m 'not e2e'"``) and run via ``make e2e``.
"""

from __future__ import annotations

import contextlib
import os
import socket
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

_HEALTHZ_TIMEOUT_S = 30.0
_TEARDOWN_WAIT_S = 5.0


def _free_port() -> int:
    """Grab a currently-free TCP port on the loopback interface.

    There is an unavoidable (tiny) race between closing the probe socket and
    the subprocess binding the port; it is fine for a local test harness.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _terminate(proc: subprocess.Popen[bytes]) -> None:
    """Terminate a subprocess, escalating to kill if it ignores SIGTERM."""
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=_TEARDOWN_WAIT_S)
    except subprocess.TimeoutExpired:
        proc.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=_TEARDOWN_WAIT_S)


@pytest.fixture(scope="session")
def live_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """Run the synthetic daemon + API server on free ports; yield the base URL.

    Both subprocesses share a ZMQ/control endpoint and a throwaway data dir via
    ``JANSKY_OBSERVE_*`` env vars, so the suite never touches real station data.
    """
    http_port = _free_port()
    zmq_port = _free_port()
    ctl_port = _free_port()
    data_dir = tmp_path_factory.mktemp("station-data")
    log_dir = tmp_path_factory.mktemp("server-logs")

    env = dict(os.environ)
    env["JANSKY_OBSERVE_ZMQ_ENDPOINT"] = f"tcp://127.0.0.1:{zmq_port}"
    # Not required by the spec, but keeps the control channel off the default
    # 8411 so a real station (or a parallel run) can't collide with the tests.
    env["JANSKY_OBSERVE_CTL_ENDPOINT"] = f"tcp://127.0.0.1:{ctl_port}"
    env["JANSKY_OBSERVE_DATA_DIR"] = str(data_dir)

    capture_log = (log_dir / "capture.log").open("wb")
    server_log = (log_dir / "server.log").open("wb")

    def _dump_logs() -> str:
        capture_log.flush()
        server_log.flush()
        cap = Path(capture_log.name).read_text(errors="replace")
        srv = Path(server_log.name).read_text(errors="replace")
        return f"\n--- capture daemon log ---\n{cap}\n--- API server log ---\n{srv}"

    daemon = subprocess.Popen(
        ["jansky-observe-capture", "--synthetic"],
        cwd=str(data_dir),
        env=env,
        stdout=capture_log,
        stderr=subprocess.STDOUT,
    )
    server = subprocess.Popen(
        ["jansky-observe", "--host", "127.0.0.1", "--port", str(http_port)],
        cwd=str(data_dir),
        env=env,
        stdout=server_log,
        stderr=subprocess.STDOUT,
    )

    base_url = f"http://127.0.0.1:{http_port}"
    try:
        deadline = time.monotonic() + _HEALTHZ_TIMEOUT_S
        while True:
            if daemon.poll() is not None or server.poll() is not None:
                raise RuntimeError(f"a subprocess exited before /healthz{_dump_logs()}")
            try:
                resp = httpx.get(f"{base_url}/healthz", timeout=2.0)
                if resp.status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            if time.monotonic() > deadline:
                raise RuntimeError(
                    f"/healthz did not come up within {_HEALTHZ_TIMEOUT_S:.0f}s{_dump_logs()}"
                )
            time.sleep(0.25)

        yield base_url
    finally:
        _terminate(server)
        _terminate(daemon)
        capture_log.close()
        server_log.close()
