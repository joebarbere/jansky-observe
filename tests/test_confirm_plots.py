"""Tests for confirm.plots: verdict and dual-axis renders under Agg."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from jansky.signals import rng

from jansky_observe.confirm.classifier import classify_spectrum
from jansky_observe.confirm.plots import dual_axis_plot, verdict_plot

HI = 1_420_405_751.7667
N = 1024
CENTER_HZ, RATE_HZ = 1420.4e6, 3e6
FREQ = CENTER_HZ + (np.arange(N) - N / 2) * (RATE_HZ / N)
WINDOW = (HI - 0.3e6, HI + 0.3e6)
C_M_PER_S = 299_792_458.0
MIN_PNG_BYTES = 10_000


def _spectrum_db(seed: int, line_amp: float) -> np.ndarray:
    """Seeded smoothed-noise spectrum (dB) with an optional Gaussian line."""
    smooth = 31
    gen = rng(seed)
    noise = np.convolve(gen.standard_normal(N + smooth - 1), np.ones(smooth) / smooth, mode="valid")
    linear = 1.0 + 0.02 * noise + line_amp * np.exp(-0.5 * ((FREQ - (HI + 120e3)) / 50e3) ** 2)
    return np.asarray(10.0 * np.log10(linear))


@pytest.mark.parametrize(
    ("seed", "line_amp", "expected_verdict"),
    [
        (0, 0.15, "detected"),
        (2, 0.010, "uncertain"),
        (2, 0.0, "not_detected"),
    ],
)
def test_verdict_plot_all_verdicts(
    tmp_path: Path, seed: int, line_amp: float, expected_verdict: str
) -> None:
    power_db = _spectrum_db(seed, line_amp)
    verdict = classify_spectrum(FREQ, power_db, window_hz=WINDOW)
    assert verdict.verdict == expected_verdict  # the fixture produces the intended case

    out = verdict_plot(FREQ, power_db, verdict, tmp_path / f"{expected_verdict}.png")
    assert out == tmp_path / f"{expected_verdict}.png"
    assert out.exists()
    assert out.stat().st_size > MIN_PNG_BYTES


def test_dual_axis_plot(tmp_path: Path) -> None:
    power_db = _spectrum_db(0, 0.15)
    # Radio-convention v_LSR axis (decreasing with frequency), as vlsr_axis returns.
    vlsr_kms = (HI - FREQ) / HI * C_M_PER_S / 1e3
    out = dual_axis_plot(FREQ, power_db, vlsr_kms, tmp_path / "dual_axis.png")
    assert out.exists()
    assert out.stat().st_size > MIN_PNG_BYTES


def test_verdict_plot_creates_parent_dirs(tmp_path: Path) -> None:
    power_db = _spectrum_db(0, 0.15)
    verdict = classify_spectrum(FREQ, power_db, window_hz=WINDOW)
    out = verdict_plot(FREQ, power_db, verdict, tmp_path / "nested" / "dir" / "plot.png")
    assert out.exists()
    assert out.stat().st_size > MIN_PNG_BYTES
