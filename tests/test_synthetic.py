"""Tests for the deterministic synthetic noise + fake-HI generator."""

from __future__ import annotations

import numpy as np

from jansky_observe.capture.dsp import welch_psd_db
from jansky_observe.synthetic import (
    DEFAULT_CENTER_FREQ_HZ,
    DEFAULT_SAMPLE_RATE_HZ,
    HI_REST_FREQ_HZ,
    hi_iq_chunk,
    rng,
)


def test_dtype_and_shape() -> None:
    chunk = hi_iq_chunk(4096, rng(0))
    assert chunk.dtype == np.complex64
    assert chunk.shape == (4096,)


def test_same_seed_is_identical() -> None:
    a = hi_iq_chunk(8192, rng(123))
    b = hi_iq_chunk(8192, rng(123))
    np.testing.assert_array_equal(a, b)


def test_different_seeds_differ() -> None:
    a = hi_iq_chunk(8192, rng(1))
    b = hi_iq_chunk(8192, rng(2))
    assert not np.array_equal(a, b)


def test_hi_bump_visible_in_welch_psd() -> None:
    n_fft = 2048
    fs = DEFAULT_SAMPLE_RATE_HZ
    chunk = hi_iq_chunk(n_fft * 32, rng(0))
    psd_db = welch_psd_db(chunk, fs, n_fft)
    # Baseband frequency axis matching the fftshifted PSD.
    freqs = (np.arange(n_fft) - n_fft / 2) * (fs / n_fft)
    line_freq = HI_REST_FREQ_HZ - DEFAULT_CENTER_FREQ_HZ  # ~ +5.75 kHz
    in_line = np.abs(freqs - line_freq) < 100e3
    off_line = np.abs(freqs - line_freq) > 500e3
    assert in_line.any() and off_line.any()
    margin_db = psd_db[in_line].mean() - psd_db[off_line].mean()
    assert margin_db > 3.0  # the fake HI line stands clearly above the floor


def test_time_wobble_changes_chunks() -> None:
    # Same generator state, different signal time: the line power wobbles.
    a = hi_iq_chunk(4096, rng(0), t0_s=0.0)
    b = hi_iq_chunk(4096, rng(0), t0_s=0.0625)  # a quarter of the wobble period
    assert not np.array_equal(a, b)
