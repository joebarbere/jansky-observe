"""Tests for confirm.classifier: the hline_v1 verdicts on synthetic spectra.

Fixtures follow the /synthetic-fixture pattern: seeded, inline, asserted
on physics (SNR thresholds, peak location, false-flag behavior), never on
golden arrays. The smoothed noise mimics a well-averaged spectrum, where
adjacent channels are correlated — that is what the classifier sees live.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest
from jansky.signals import rng

from jansky_observe import synthetic
from jansky_observe.capture.dsp import welch_psd_db
from jansky_observe.capture.writer import NpzCaptureWriter
from jansky_observe.confirm.classifier import (
    CLASSIFIER_NAME,
    CLASSIFIER_VERSION,
    averaged_spectrum,
    classify_capture_npz,
    classify_spectrum,
    running_classify,
)
from jansky_observe.frames import SpectralFrame

HI = synthetic.HI_REST_FREQ_HZ
N = 1024
CENTER_HZ, RATE_HZ = 1420.4e6, 3e6
FREQ = CENTER_HZ + (np.arange(N) - N / 2) * (RATE_HZ / N)
CHANNEL_HZ = RATE_HZ / N
WINDOW = (HI - 0.3e6, HI + 0.3e6)
LAT, LON, ELEV_M = 40.02, -75.16, 100.0


def _spectrum_db(
    seed: int, line_amp: float, *, offset_hz: float = 0.0, width_hz: float = 50e3
) -> np.ndarray:
    """Seeded smoothed-noise spectrum (dB) with an optional Gaussian line."""
    smooth = 31
    gen = rng(seed)
    noise = np.convolve(gen.standard_normal(N + smooth - 1), np.ones(smooth) / smooth, mode="valid")
    linear = 1.0 + 0.02 * noise
    linear += line_amp * np.exp(-0.5 * ((FREQ - (HI + offset_hz)) / width_hz) ** 2)
    return np.asarray(10.0 * np.log10(linear))


class TestClassifySpectrum:
    def test_strong_line_detected_at_injected_offset(self) -> None:
        offset_hz = 120e3
        verdict = classify_spectrum(
            FREQ, _spectrum_db(0, 0.15, offset_hz=offset_hz), window_hz=WINDOW
        )
        assert verdict.verdict == "detected"
        assert verdict.score >= 5.0
        assert verdict.params["peak_freq_hz"] == pytest.approx(HI + offset_hz, abs=CHANNEL_HZ)

    def test_no_line_not_detected(self) -> None:
        verdict = classify_spectrum(FREQ, _spectrum_db(2, 0.0), window_hz=WINDOW)
        assert verdict.verdict == "not_detected"
        assert verdict.score < 2.0

    def test_marginal_line_uncertain(self) -> None:
        verdict = classify_spectrum(FREQ, _spectrum_db(2, 0.010, offset_hz=120e3), window_hz=WINDOW)
        assert verdict.verdict == "uncertain"
        assert 2.0 <= verdict.score < 5.0

    def test_rfi_spike_outside_window_not_flagged(self) -> None:
        power_db = _spectrum_db(2, 0.0)
        spike_index = int(np.argmin(np.abs(FREQ - (HI - 1.0e6))))  # well outside the window
        power_db[spike_index] += 30.0
        verdict = classify_spectrum(FREQ, power_db, window_hz=WINDOW)
        assert verdict.verdict != "detected"

    def test_deterministic(self) -> None:
        a = classify_spectrum(FREQ, _spectrum_db(0, 0.15, offset_hz=120e3), window_hz=WINDOW)
        b = classify_spectrum(FREQ, _spectrum_db(0, 0.15, offset_hz=120e3), window_hz=WINDOW)
        assert a == b
        assert a.score == b.score
        assert a.params == b.params

    def test_provenance_fields_and_params(self) -> None:
        verdict = classify_spectrum(FREQ, _spectrum_db(0, 0.15), window_hz=WINDOW)
        assert verdict.name == CLASSIFIER_NAME == "hline_v1"
        assert verdict.version == CLASSIFIER_VERSION == "1"
        for key in (
            "peak_freq_hz",
            "peak_snr",
            "baseline_rms",
            "baseline_order",
            "window_hz",
            "n_channels",
        ):
            assert key in verdict.params
        assert verdict.params["n_channels"] == N
        json.dumps(verdict.params)  # JSON-able, as ClassifierResult rows require

    def test_empty_window_raises(self) -> None:
        with pytest.raises(ValueError, match="no channels inside"):
            classify_spectrum(FREQ, _spectrum_db(0, 0.0), window_hz=(HI + 10e6, HI + 11e6))

    def test_running_classify_matches_classify_spectrum(self) -> None:
        power_db = _spectrum_db(0, 0.15, offset_hz=120e3)
        assert running_classify(FREQ, power_db, WINDOW) == classify_spectrum(
            FREQ, power_db, window_hz=WINDOW
        )


def _write_synthetic_capture(path: Path, n_frames: int = 8, n_fft: int = 256) -> Path:
    """Write an .npz capture of synthetic HI frames via the real writer."""
    gen = rng(7)
    writer = NpzCaptureWriter(path, settings={"gain": 15, "source": "synthetic"})
    samples_per_frame = n_fft * 32
    for i in range(n_frames):
        iq = synthetic.hi_iq_chunk(
            samples_per_frame,
            gen,
            t0_s=i * samples_per_frame / RATE_HZ,
            center_freq_hz=CENTER_HZ,
            sample_rate_hz=RATE_HZ,
        )
        writer.add_frame(
            SpectralFrame(
                seq=i,
                timestamp=1_750_000_000.0 + i * 0.5,
                center_freq_hz=CENTER_HZ,
                sample_rate_hz=RATE_HZ,
                power_db=welch_psd_db(iq, RATE_HZ, n_fft),
            )
        )
    return writer.close()


class TestClassifyCaptureNpz:
    def test_end_to_end_fixed_window(self, tmp_path: Path) -> None:
        npz = _write_synthetic_capture(tmp_path / "capture.npz")
        verdict = classify_capture_npz(npz, lat_deg=LAT, lon_deg=LON, elevation_m=ELEV_M)
        assert verdict.verdict == "detected"
        assert verdict.params["window_source"] == "fixed"
        # The synthetic line sits at the HI rest frequency (no offset).
        assert verdict.params["peak_freq_hz"] == pytest.approx(HI, abs=50e3)
        round_tripped = json.loads(json.dumps(verdict.params))
        assert round_tripped == verdict.params

    def test_end_to_end_lsr_window(self, tmp_path: Path) -> None:
        from jansky_observe.astro.pointing import target_coord

        npz = _write_synthetic_capture(tmp_path / "capture.npz")
        verdict = classify_capture_npz(
            npz,
            lat_deg=LAT,
            lon_deg=LON,
            elevation_m=ELEV_M,
            coord=target_coord("radec", ra_deg=299.8682, dec_deg=40.7339),
            when=datetime(2026, 1, 15, 0, 0, 0, tzinfo=UTC),
        )
        assert verdict.params["window_source"] == "lsr"
        assert verdict.verdict == "detected"

    def test_averaged_spectrum_axis_and_shape(self, tmp_path: Path) -> None:
        npz = _write_synthetic_capture(tmp_path / "capture.npz", n_fft=256)
        freq_hz, power_db = averaged_spectrum(npz)
        assert freq_hz.shape == power_db.shape == (256,)
        # fftshifted frame convention: index 0 = center - rate/2.
        assert freq_hz[0] == pytest.approx(CENTER_HZ - RATE_HZ / 2)
        assert np.all(np.diff(freq_hz) > 0)
        assert np.isfinite(power_db).all()
