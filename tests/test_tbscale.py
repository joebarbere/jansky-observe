"""Relative dB -> kelvin (plans/calibrated-overlay.md)."""

from __future__ import annotations

import numpy as np
import pytest

from jansky_observe.astro.lsr import HI_LINE_FREQ_HZ
from jansky_observe.confirm.baseline import linear_to_db
from jansky_observe.confirm.tbscale import brightness_temperature

RATE_HZ = 2.0e6
WINDOW = (HI_LINE_FREQ_HZ - 3.0e5, HI_LINE_FREQ_HZ + 3.0e5)


def _spectrum(line_k: float, tsys_k: float, n: int = 512) -> tuple[np.ndarray, np.ndarray]:
    """A flat-baseline spectrum in dB carrying a Gaussian line of ``line_k`` kelvin.

    The receiver sees ``Tsys + T_A(v)``; with a flat gain the recorded linear power is
    proportional to that sum, so a unit-gain construction is exact and the scaling is
    the thing under test.
    """
    freq = HI_LINE_FREQ_HZ + np.linspace(-RATE_HZ / 2, RATE_HZ / 2, n)
    sigma = 1.0e5
    line = line_k * np.exp(-0.5 * ((freq - HI_LINE_FREQ_HZ) / sigma) ** 2)
    return freq, linear_to_db(tsys_k + line)


def test_recovers_an_injected_line_amplitude() -> None:
    freq, db = _spectrum(line_k=60.0, tsys_k=150.0)
    scale = brightness_temperature(freq, db, tsys_k=150.0, exclude=WINDOW)
    assert scale.peak_k == pytest.approx(60.0, rel=0.02)
    assert scale.tsys_k == 150.0


def test_eta_mb_divides_and_relabels_the_axis() -> None:
    freq, db = _spectrum(line_k=60.0, tsys_k=150.0)
    uncorrected = brightness_temperature(freq, db, tsys_k=150.0, exclude=WINDOW)
    corrected = brightness_temperature(freq, db, tsys_k=150.0, exclude=WINDOW, eta_mb=0.65)

    assert corrected.peak_k == pytest.approx(uncorrected.peak_k / 0.65, rel=1e-6)
    # The label is the honesty guarantee: T_A until an efficiency is applied.
    assert uncorrected.is_main_beam is False and uncorrected.axis_label == "T_A (K)"
    assert corrected.is_main_beam is True and corrected.axis_label == "T_B (K)"


def test_line_free_channels_sit_at_zero_kelvin() -> None:
    freq, db = _spectrum(line_k=60.0, tsys_k=150.0)
    scale = brightness_temperature(freq, db, tsys_k=150.0, exclude=WINDOW)
    off_line = np.abs(freq - HI_LINE_FREQ_HZ) > 6.0e5
    assert np.allclose(scale.temperature_k[off_line], 0.0, atol=0.5)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"tsys_k": 0.0}, "tsys_k must be positive"),
        ({"tsys_k": 150.0, "eta_mb": 0.0}, "eta_mb must be positive"),
    ],
)
def test_rejects_unphysical_inputs(kwargs: dict[str, float], message: str) -> None:
    freq, db = _spectrum(line_k=10.0, tsys_k=150.0)
    with pytest.raises(ValueError, match=message):
        brightness_temperature(freq, db, exclude=WINDOW, **kwargs)  # type: ignore[arg-type]


def test_rejects_mismatched_axes() -> None:
    freq, db = _spectrum(line_k=10.0, tsys_k=150.0)
    with pytest.raises(ValueError, match="shapes must match"):
        brightness_temperature(freq[:-1], db, tsys_k=150.0, exclude=WINDOW)
