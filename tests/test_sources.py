"""Tests for the SDR source protocol and the synthetic source."""

from __future__ import annotations

import numpy as np
import pytest

from jansky_observe.capture.sources import SDRSource, SyntheticHISource


def test_synthetic_source_satisfies_protocol() -> None:
    source = SyntheticHISource()
    assert isinstance(source, SDRSource)
    assert source.center_freq_hz == 1420.4e6
    assert source.sample_rate_hz == 3e6


def test_read_returns_complex64_of_requested_length() -> None:
    source = SyntheticHISource(seed=0)
    chunk = source.read(4096)
    assert chunk.dtype == np.complex64
    assert chunk.shape == (4096,)


def test_same_seed_same_stream() -> None:
    a = SyntheticHISource(seed=99)
    b = SyntheticHISource(seed=99)
    for _ in range(3):
        np.testing.assert_array_equal(a.read(2048), b.read(2048))


def test_successive_reads_differ() -> None:
    source = SyntheticHISource(seed=0)
    first = source.read(2048)
    second = source.read(2048)
    assert not np.array_equal(first, second)


def test_read_after_close_raises() -> None:
    source = SyntheticHISource(seed=0)
    source.close()
    with pytest.raises(RuntimeError, match="closed"):
        source.read(16)
