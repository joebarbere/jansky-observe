"""Polynomial baseline fitting for the v1 spectrum classifier (plan §6).

The baseline is fit in LINEAR power, not dB — convert with
:func:`db_to_linear` before calling :func:`fit_baseline` (and back with
:func:`linear_to_db` for display). Fitting in dB would bias the fit
because averaging and RMS are only meaningful on linear power.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

__all__ = ["BaselineFit", "db_to_linear", "fit_baseline", "linear_to_db"]

_LINEAR_FLOOR = 1e-20
"""Smallest linear power passed to log10, i.e. a -200 dB floor (avoids -inf)."""


def db_to_linear(power_db: np.ndarray) -> np.ndarray:
    """Convert power in dB to linear power."""
    return np.asarray(10.0 ** (np.asarray(power_db, dtype=np.float64) / 10.0))


def linear_to_db(power: np.ndarray) -> np.ndarray:
    """Convert linear power to dB, floored to avoid ``-inf``."""
    return np.asarray(
        10.0 * np.log10(np.maximum(np.asarray(power, dtype=np.float64), _LINEAR_FLOOR))
    )


@dataclass(frozen=True)
class BaselineFit:
    """A fitted polynomial baseline.

    ``baseline`` is evaluated over *all* input channels (including the
    excluded window); ``residual_rms`` comes from the fit channels only.
    """

    baseline: np.ndarray
    residual_rms: float


def fit_baseline(
    freq_hz: np.ndarray,
    power: np.ndarray,
    exclude: tuple[float, float],
    order: int = 3,
) -> BaselineFit:
    """Least-squares polynomial baseline over channels outside a window.

    Parameters
    ----------
    freq_hz : numpy.ndarray
        Frequency axis in Hz.
    power : numpy.ndarray
        LINEAR power per channel (convert dB with :func:`db_to_linear`
        first).
    exclude : tuple of float
        ``(lo_hz, hi_hz)`` window excluded from the fit — the signal
        region the baseline must not absorb.
    order : int
        Polynomial order (default 3).

    Returns
    -------
    BaselineFit
        Baseline evaluated over all channels, plus the RMS of the fit
        residuals on the fit (outside-window) channels only.

    Raises
    ------
    ValueError
        If fewer than ``order + 1`` channels remain outside the window.
    """
    freq = np.asarray(freq_hz, dtype=np.float64)
    lin = np.asarray(power, dtype=np.float64)
    lo_hz, hi_hz = exclude
    fit_mask = (freq < lo_hz) | (freq > hi_hz)
    n_fit = int(fit_mask.sum())
    if n_fit < order + 1:
        raise ValueError(
            f"need at least {order + 1} channels outside the exclusion window, got {n_fit}"
        )

    # Normalize x to [-1, 1] for polyfit conditioning at ~1.42e9 Hz scales.
    mid = 0.5 * (freq[0] + freq[-1])
    half_span = max(0.5 * abs(freq[-1] - freq[0]), 1.0)
    x = (freq - mid) / half_span

    coeffs = np.polyfit(x[fit_mask], lin[fit_mask], order)
    baseline = np.polyval(coeffs, x)
    residuals = lin[fit_mask] - baseline[fit_mask]
    return BaselineFit(
        baseline=baseline,
        residual_rms=float(np.sqrt(np.mean(residuals**2))),
    )
