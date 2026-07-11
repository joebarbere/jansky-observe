"""Tests for the spectral-frame wire formats (frames.py)."""

from __future__ import annotations

import json
import struct

import numpy as np
import pytest

from jansky_observe.frames import SCHEMA_VERSION, SpectralFrame, decode_zmq, encode_zmq, pack_ws


def _frame(n_fft: int = 16) -> SpectralFrame:
    power = np.linspace(-80.0, -20.0, n_fft, dtype=np.float32)
    return SpectralFrame(
        seq=7,
        timestamp=1_752_000_000.25,
        center_freq_hz=1420.4e6,
        sample_rate_hz=3e6,
        power_db=power,
    )


def test_encode_decode_zmq_round_trip() -> None:
    frame = _frame()
    parts = encode_zmq(frame)
    assert len(parts) == 2
    out = decode_zmq(parts)
    assert out.seq == frame.seq
    assert out.timestamp == frame.timestamp
    assert out.center_freq_hz == frame.center_freq_hz
    assert out.sample_rate_hz == frame.sample_rate_hz
    assert out.power_db.dtype == np.float32
    np.testing.assert_array_equal(out.power_db, frame.power_db)


def test_pack_ws_layout() -> None:
    frame = _frame(n_fft=32)
    blob = pack_ws(frame)
    (header_len,) = struct.unpack_from("<I", blob, 0)
    header = json.loads(blob[4 : 4 + header_len].decode())
    payload = np.frombuffer(blob[4 + header_len :], dtype="<f4")
    assert header["v"] == SCHEMA_VERSION
    assert header["seq"] == frame.seq
    assert header["timestamp"] == frame.timestamp
    assert header["center_freq_hz"] == frame.center_freq_hz
    assert header["sample_rate_hz"] == frame.sample_rate_hz
    assert header["n_fft"] == 32
    np.testing.assert_array_equal(payload, frame.power_db)


def test_decode_zmq_rejects_wrong_part_count() -> None:
    with pytest.raises(ValueError, match="2 message parts"):
        decode_zmq([b"{}"])


def test_decode_zmq_rejects_unknown_schema_version() -> None:
    header, payload = encode_zmq(_frame())
    bad = json.loads(header)
    bad["v"] = SCHEMA_VERSION + 1
    with pytest.raises(ValueError, match="schema version"):
        decode_zmq([json.dumps(bad).encode(), payload])


def test_decode_zmq_rejects_payload_size_mismatch() -> None:
    header, payload = encode_zmq(_frame())
    with pytest.raises(ValueError, match="bins"):
        decode_zmq([header, payload[:-4]])


def test_frequencies_hz_endpoints() -> None:
    frame = _frame(n_fft=8)
    freqs = frame.frequencies_hz()
    n = 8
    fs = frame.sample_rate_hz
    assert freqs.size == n
    assert freqs[0] == pytest.approx(frame.center_freq_hz - fs / 2)
    assert freqs[-1] == pytest.approx(frame.center_freq_hz + fs / 2 - fs / n)
    assert np.all(np.diff(freqs) > 0)
