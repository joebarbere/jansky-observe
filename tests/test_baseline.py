"""Tests for confirm.baseline: polynomial baseline fitting on linear power."""

from __future__ import annotations

import numpy as np
import pytest

from jansky_observe.confirm.baseline import db_to_linear, fit_baseline, linear_to_db

HI = 1_420_405_751.7667
N = 512
FREQ = 1420.4e6 + (np.arange(N) - N / 2) * (3e6 / N)
WINDOW = (HI - 0.3e6, HI + 0.3e6)


def _cubic_power() -> np.ndarray:
    """A noiseless cubic baseline in linear power (positive over the band)."""
    x = (FREQ - FREQ.mean()) / (0.5 * (FREQ[-1] - FREQ[0]))
    return 2.0 + 0.3 * x + 0.2 * x**2 - 0.1 * x**3


class TestFitBaseline:
    def test_cubic_ripple_fits_exactly(self) -> None:
        fit = fit_baseline(FREQ, _cubic_power(), exclude=WINDOW, order=3)
        assert fit.residual_rms < 1e-9
        # The evaluated baseline tracks the true shape everywhere,
        # including inside the excluded window.
        assert np.allclose(fit.baseline, _cubic_power(), atol=1e-9)

    def test_exclusion_window_really_excluded(self) -> None:
        clean = _cubic_power()
        spiked = clean.copy()
        in_window = (FREQ >= WINDOW[0]) & (FREQ <= WINDOW[1])
        spiked[in_window] += 100.0  # a huge line inside the window
        fit_clean = fit_baseline(FREQ, clean, exclude=WINDOW, order=3)
        fit_spiked = fit_baseline(FREQ, spiked, exclude=WINDOW, order=3)
        assert np.allclose(fit_spiked.baseline, fit_clean.baseline, atol=1e-8)
        assert fit_spiked.residual_rms == pytest.approx(fit_clean.residual_rms, abs=1e-9)

    def test_noisy_flat_rms_matches_noise(self) -> None:
        rng = np.random.default_rng(11)
        noise_sigma = 0.05
        power = 1.0 + noise_sigma * rng.standard_normal(N)
        fit = fit_baseline(FREQ, power, exclude=WINDOW, order=3)
        assert fit.residual_rms == pytest.approx(noise_sigma, rel=0.2)

    def test_too_few_fit_channels_raises(self) -> None:
        with pytest.raises(ValueError, match="outside the exclusion window"):
            fit_baseline(FREQ, _cubic_power(), exclude=(FREQ[0] - 1.0, FREQ[-1] + 1.0), order=3)


class TestDbConversions:
    def test_round_trip(self) -> None:
        db = np.array([-30.0, -3.0, 0.0, 3.0, 10.0])
        assert np.allclose(linear_to_db(db_to_linear(db)), db, atol=1e-12)

    def test_linear_to_db_floors_zero(self) -> None:
        out = linear_to_db(np.array([0.0, 1.0]))
        assert np.isfinite(out).all()
        assert out[0] == pytest.approx(-200.0)
        assert out[1] == pytest.approx(0.0)
