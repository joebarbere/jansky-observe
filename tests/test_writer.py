"""Tests for the capture writers: .npz round-trip and SigMF interop with jansky."""

from __future__ import annotations

import json

import numpy as np
import pytest
from jansky import formats

from jansky_observe import __version__
from jansky_observe.capture.writer import NpzCaptureWriter, SigmfCaptureWriter
from jansky_observe.frames import SpectralFrame


def _frame(seq: int, n_fft: int = 64) -> SpectralFrame:
    return SpectralFrame(
        seq=seq,
        timestamp=1e9 + seq,
        center_freq_hz=1420.4e6,
        sample_rate_hz=3e6,
        power_db=np.full(n_fft, float(seq), dtype=np.float32),
    )


def test_npz_round_trip(tmp_path):
    writer = NpzCaptureWriter(tmp_path / "cap.npz", {"gain": 16})
    for seq in range(5):
        writer.add_frame(_frame(seq))
    path = writer.close()
    data = np.load(path)
    assert data["power_db"].shape == (5, 64)
    assert np.all(data["power_db"][3] == 3.0)
    assert data["timestamps"].shape == (5,)
    assert float(data["center_freq_hz"]) == 1420.4e6
    settings = json.loads(str(data["settings"]))
    assert settings["gain"] == 16
    assert settings["software_version"] == __version__
    assert writer.bytes_written == path.stat().st_size


def test_npz_empty_capture_raises(tmp_path):
    writer = NpzCaptureWriter(tmp_path / "cap.npz")
    with pytest.raises(RuntimeError, match="no frames"):
        writer.close()


def test_sigmf_streams_int16_and_reads_back_via_jansky(tmp_path):
    base = tmp_path / "cap"
    writer = SigmfCaptureWriter(
        base, sample_rate_hz=3e6, center_freq_hz=1420.4e6, settings={"gain": 16}
    )
    first = np.arange(0, 1000, dtype=np.int16)
    second = np.arange(1000, 2000, dtype=np.int16)
    writer.write(first)
    writer.write(second)
    assert writer.bytes_written == 4000
    meta_path = writer.close()

    samples, meta = formats.read_sigmf(base)
    raw = np.asarray(samples).view(np.int16).ravel()
    assert np.array_equal(raw[:2000], np.arange(2000, dtype=np.int16))
    g = meta["global"]
    assert g["core:datatype"] == "ci16_le"
    assert g["core:sample_rate"] == 3e6
    assert g["jansky_observe:settings"]["gain"] == 16
    assert g["jansky_observe:version"] == __version__
    assert meta["captures"][0]["core:frequency"] == 1420.4e6
    assert meta_path.exists()
