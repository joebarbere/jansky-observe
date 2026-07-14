"""Tests for the Welch PSD reduction (capture/dsp.py)."""

from __future__ import annotations

import numpy as np
import pytest
from scipy import signal

from jansky_observe.capture.dsp import welch_psd_db


def test_tone_peaks_in_expected_fftshifted_bin() -> None:
    fs = 1e6
    n_fft = 256
    tone_freq = 125e3  # exactly bin 32 above center
    t = np.arange(n_fft * 8) / fs
    iq = np.exp(2j * np.pi * tone_freq * t).astype(np.complex64)
    psd_db = welch_psd_db(iq, fs, n_fft)
    expected_bin = n_fft // 2 + round(tone_freq / fs * n_fft)
    assert int(np.argmax(psd_db)) == expected_bin


def test_negative_frequency_tone() -> None:
    fs = 1e6
    n_fft = 256
    tone_freq = -250e3
    t = np.arange(n_fft * 4) / fs
    iq = np.exp(2j * np.pi * tone_freq * t).astype(np.complex64)
    psd_db = welch_psd_db(iq, fs, n_fft)
    expected_bin = n_fft // 2 + round(tone_freq / fs * n_fft)
    assert int(np.argmax(psd_db)) == expected_bin


def test_output_dtype_and_length() -> None:
    iq = np.zeros(1024, dtype=np.complex64)
    psd_db = welch_psd_db(iq, 3e6, 512)
    assert psd_db.dtype == np.float32
    assert psd_db.shape == (512,)
    assert np.isfinite(psd_db).all()  # zero input hits the floor, never -inf


def test_rejects_short_input() -> None:
    with pytest.raises(ValueError, match="n_fft"):
        welch_psd_db(np.zeros(100, dtype=np.complex64), 3e6, 256)


def test_precomputed_window_is_bit_identical_to_string_window() -> None:
    """The cached window array must give results identical to scipy's string window.

    The classifier's SNR thresholds are calibrated against these exact dB values,
    so the per-frame window-caching optimization must change nothing numerically.
    """
    rng = np.random.default_rng(1420)
    iq = (rng.standard_normal(4096) + 1j * rng.standard_normal(4096)).astype(np.complex64)
    got = welch_psd_db(iq, 3e6, 512)
    _, ref = signal.welch(
        iq,
        fs=3e6,
        window="hann",
        nperseg=512,
        noverlap=0,
        detrend=False,
        return_onesided=False,
        scaling="density",
    )
    ref_db = (10.0 * np.log10(np.maximum(np.fft.fftshift(ref), 1e-20))).astype(np.float32)
    assert np.array_equal(got, ref_db)
