"""Tests for the airspy_rx subprocess source — via the fake binary, no hardware."""

from __future__ import annotations

import dataclasses
import sys
from pathlib import Path

import numpy as np
import pytest

from jansky_observe.capture.airspy_cli import AirspyRxSource, build_airspy_cmd
from jansky_observe.capture.profiles import HLINE_AIRSPY, BiasTeeForbiddenError

FAKE = [sys.executable, str(Path(__file__).parent / "fake_airspy_rx.py")]
WRAP = 16384


def test_build_cmd_never_emits_bias_tee():
    cmd = build_airspy_cmd(HLINE_AIRSPY)
    assert "-b" not in cmd
    assert cmd[0] == "airspy_rx"
    assert "-t" in cmd and cmd[cmd.index("-t") + 1] == "2"
    assert cmd[cmd.index("-f") + 1] == "1420.4000"
    assert cmd[cmd.index("-a") + 1] == "3000000"


def test_build_cmd_rejects_bias_tee_profile():
    bad = dataclasses.replace(HLINE_AIRSPY, bias_tee=True)
    with pytest.raises(BiasTeeForbiddenError):
        build_airspy_cmd(bad)


def test_build_cmd_rejects_bad_gain():
    with pytest.raises(ValueError):
        build_airspy_cmd(HLINE_AIRSPY, gain=22)


def test_missing_binary_error_names_package():
    with pytest.raises(RuntimeError, match="apt package"):
        AirspyRxSource(binary="/nonexistent/airspy_rx")


def test_read_returns_complex64():
    source = AirspyRxSource(binary=FAKE)
    try:
        iq = source.read(4096)
        assert iq.dtype == np.complex64
        assert iq.shape == (4096,)
        assert np.all(np.abs(iq) <= np.sqrt(2))
    finally:
        source.close()


def test_tap_is_gapless_while_live_reads_happen():
    source = AirspyRxSource(binary=FAKE, tap_max_seconds=8.0)
    try:
        source.start_tap()
        collected = []
        for _ in range(5):
            source.read(2048)  # live view keeps being served
            values = source.read_tap()
            if values is not None:
                collected.append(values)
        while sum(v.size for v in collected) < 100_000:
            values = source.read_tap()
            if values is not None:
                collected.append(values)
        source.stop_tap()
        stream = np.concatenate(collected).astype(np.int64)
        diffs = np.diff(stream) % WRAP
        assert stream.size >= 100_000
        assert np.all(diffs == 1), "tap stream has gaps or duplicates"
        assert source.overrun is False
    finally:
        source.close()


def test_dead_subprocess_raises():
    source = AirspyRxSource(binary=FAKE)
    source._proc.kill()
    source._proc.wait()
    with pytest.raises(RuntimeError, match="exited"):
        # drain whatever is buffered, then hit the dead pipe
        for _ in range(1000):
            source.read(65536)
    source.close()
