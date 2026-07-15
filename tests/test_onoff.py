"""Tests for confirm.onoff (ON−OFF difference) and classify_difference_npz.

Fixtures follow the /synthetic-fixture pattern: seeded, inline, asserted on
physics (a bump at the injected bin, a flat difference for identical inputs,
SNR thresholds), never on golden arrays.
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
from jansky_observe.confirm.baseline import db_to_linear
from jansky_observe.confirm.classifier import (
    CLASSIFIER_ONOFF_NAME,
    CLASSIFIER_VERSION,
    classify_difference_npz,
)
from jansky_observe.confirm.onoff import difference_spectrum
from jansky_observe.frames import SpectralFrame

HI = synthetic.HI_REST_FREQ_HZ
N = 1024
CENTER_HZ, RATE_HZ = 1420.4e6, 3e6
FREQ = CENTER_HZ + (np.arange(N) - N / 2) * (RATE_HZ / N)
CHANNEL_HZ = RATE_HZ / N
LAT, LON, ELEV_M = 40.02, -75.16, 100.0


def _flat_db(seed: int) -> np.ndarray:
    """A seeded, near-flat spectrum in dB (bandpass + small noise, no line)."""
    gen = rng(seed)
    smooth = 31
    noise = np.convolve(gen.standard_normal(N + smooth - 1), np.ones(smooth) / smooth, mode="valid")
    linear = 1.0 + 0.02 * noise
    return np.asarray(10.0 * np.log10(linear))


def _line_db(base_db: np.ndarray, amp: float, *, offset_hz: float = 0.0) -> np.ndarray:
    """Add a Gaussian HI line (in linear power) onto a base dB spectrum."""
    linear = db_to_linear(base_db)
    linear = linear + amp * np.exp(-0.5 * ((FREQ - (HI + offset_hz)) / 50e3) ** 2)
    return np.asarray(10.0 * np.log10(linear))


class TestDifferenceSpectrum:
    def test_ratio_recovers_line_bump_at_injected_bin(self) -> None:
        offset_hz = 120e3
        off_db = _flat_db(1)
        on_db = _line_db(off_db, 0.5, offset_hz=offset_hz)  # same bandpass, ON adds the line
        freq, diff_db = difference_spectrum(FREQ, on_db, FREQ, off_db, method="ratio")
        assert freq is FREQ or np.allclose(freq, FREQ)
        # The ratio is ~0 dB everywhere but bumps up at the injected bin.
        peak_bin = int(np.argmax(diff_db))
        assert freq[peak_bin] == pytest.approx(HI + offset_hz, abs=2 * CHANNEL_HZ)
        assert diff_db[peak_bin] > 1.0  # a clear positive bump (dB)
        # Away from the line the difference is essentially flat.
        away = np.abs(FREQ - (HI + offset_hz)) > 0.5e6
        assert np.max(np.abs(diff_db[away])) < 0.5

    def test_identical_on_off_is_flat(self) -> None:
        spectrum = _flat_db(3)
        _, diff_db = difference_spectrum(FREQ, spectrum, FREQ, spectrum, method="ratio")
        assert np.allclose(diff_db, 0.0, atol=1e-9)

    def test_subtract_method_floors_and_bumps(self) -> None:
        off_db = _flat_db(4)
        on_db = _line_db(off_db, 0.5, offset_hz=0.0)
        _, diff_db = difference_spectrum(FREQ, on_db, FREQ, off_db, method="subtract")
        # ON − OFF isolates the line's linear power; the peak sits at the line.
        assert int(np.argmax(diff_db)) == pytest.approx(int(np.argmin(np.abs(FREQ - HI))), abs=2)
        assert np.all(np.isfinite(diff_db))

    def test_shape_mismatch_raises(self) -> None:
        with pytest.raises(ValueError, match="shapes"):
            difference_spectrum(FREQ, _flat_db(1), FREQ[:-1], _flat_db(1)[:-1])

    def test_axis_value_mismatch_raises(self) -> None:
        shifted = FREQ + 5e6  # a retune: same shape, different frequencies
        with pytest.raises(ValueError, match="frequency axes differ"):
            difference_spectrum(FREQ, _flat_db(1), shifted, _flat_db(2))

    def test_unknown_method_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown difference method"):
            difference_spectrum(FREQ, _flat_db(1), FREQ, _flat_db(1), method="divide")


def _write_capture(
    path: Path, *, line_amplitude: float, seed: int, n_frames: int = 8, n_fft: int = 256
) -> Path:
    """Write an .npz capture of synthetic frames (optionally with an HI line)."""
    gen = rng(seed)
    writer = NpzCaptureWriter(path, settings={"gain": 15, "source": "synthetic"})
    samples_per_frame = n_fft * 32
    for i in range(n_frames):
        iq = synthetic.hi_iq_chunk(
            samples_per_frame,
            gen,
            t0_s=i * samples_per_frame / RATE_HZ,
            center_freq_hz=CENTER_HZ,
            sample_rate_hz=RATE_HZ,
            line_amplitude=line_amplitude,
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


class TestClassifyDifferenceNpz:
    def test_strong_line_pair_detected(self, tmp_path: Path) -> None:
        on = _write_capture(tmp_path / "on.npz", line_amplitude=3.0, seed=1)
        off = _write_capture(tmp_path / "off.npz", line_amplitude=0.0, seed=2)
        verdict = classify_difference_npz(on, off, lat_deg=LAT, lon_deg=LON, elevation_m=ELEV_M)
        assert verdict.name == CLASSIFIER_ONOFF_NAME == "hline_v1_onoff"
        assert verdict.version == CLASSIFIER_VERSION == "1"
        assert verdict.verdict == "detected"
        assert verdict.score >= 5.0
        assert verdict.params["method"] == "ratio"
        assert verdict.params["window_source"] == "fixed"
        assert verdict.params["peak_freq_hz"] == pytest.approx(HI, abs=100e3)
        json.loads(json.dumps(verdict.params))  # JSON-able (provenance rows serialize params)

    def test_no_line_pair_not_detected(self, tmp_path: Path) -> None:
        # Independent noise realizations (distinct seeds) — a genuine blank-field
        # ON/OFF ratio, no shared line; the difference has no in-window bump.
        on = _write_capture(tmp_path / "on.npz", line_amplitude=0.0, seed=5)
        off = _write_capture(tmp_path / "off.npz", line_amplitude=0.0, seed=6)
        verdict = classify_difference_npz(on, off, lat_deg=LAT, lon_deg=LON, elevation_m=ELEV_M)
        assert verdict.verdict == "not_detected"
        assert verdict.score < 2.0

    def test_lsr_window_when_pointing_given(self, tmp_path: Path) -> None:
        from jansky_observe.astro.pointing import target_coord

        on = _write_capture(tmp_path / "on.npz", line_amplitude=3.0, seed=1)
        off = _write_capture(tmp_path / "off.npz", line_amplitude=0.0, seed=2)
        verdict = classify_difference_npz(
            on,
            off,
            lat_deg=LAT,
            lon_deg=LON,
            elevation_m=ELEV_M,
            coord=target_coord("radec", ra_deg=299.8682, dec_deg=40.7339),
            when=datetime(2026, 1, 15, 0, 0, 0, tzinfo=UTC),
        )
        assert verdict.params["window_source"] == "lsr"
        assert verdict.verdict == "detected"

    def test_axis_mismatch_raises(self, tmp_path: Path) -> None:
        on = _write_capture(tmp_path / "on.npz", line_amplitude=3.0, seed=1, n_fft=256)
        off = _write_capture(tmp_path / "off.npz", line_amplitude=0.0, seed=2, n_fft=128)
        with pytest.raises(ValueError, match="axis mismatch"):
            classify_difference_npz(on, off, lat_deg=LAT, lon_deg=LON, elevation_m=ELEV_M)
