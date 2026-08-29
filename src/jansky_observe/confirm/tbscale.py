"""Relative power -> antenna temperature, given a Tsys (plans/calibrated-overlay.md).

The M12 overlay draws observed **relative dB** against model **brightness temperature
(K)** on two independent axes, so it compares *shape* only. A pure scale error --- the
observed line coming out at a constant fraction of the model's --- is invisible in that
view: the curves line up and the station looks calibrated. Putting both traces on one
kelvin axis is what makes such an error visible, and M10's sky/ground Y-factor
(:mod:`jansky_observe.confirm.skyground`) already supplies the missing constant.

The calibration is the standard single-load one::

    T_A(v) = tsys_k * (P(v) / B(v) - 1)
    T_B(v) = T_A(v) / eta_mb

where ``P`` is linear power and ``B`` is a polynomial baseline fitted over channels
*outside* the HI Doppler window (so the line is never absorbed into it).

**The assumption, stated because it is the thing that breaks first:** line-free channels
carry ``Tsys`` alone, and any excess above the baseline is sky signal. A receiver whose
gain drifts or is strongly non-flat across the band violates this, and then the kelvin
axis is only as good as the baseline fit under it.

**``eta_mb`` defaults to 1.0, and at 1.0 the result is antenna temperature, not
brightness temperature.** Main-beam efficiency for a small dish with a simple feed is
typically 0.5-0.7, so labelling an uncorrected ``T_A`` as ``T_B`` builds a ~1.5x error
into every number derived from it. :attr:`TemperatureScale.is_main_beam` exists so
callers label the axis from the data rather than from an assumption.

Advisory analysis, never a verdict (plan section 12.5).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from jansky_observe.confirm.baseline import db_to_linear, fit_baseline

__all__ = ["TemperatureScale", "brightness_temperature"]

#: Baseline polynomial order, matching the v1 classifier's.
BASELINE_ORDER = 3


@dataclass(frozen=True)
class TemperatureScale:
    """A spectrum on a kelvin axis, plus what it took to get there.

    ``temperature_k`` is antenna temperature when :attr:`is_main_beam` is False (the
    default) and beam-corrected brightness temperature when it is True. ``baseline_rms``
    is the linear-power RMS of the baseline fit residual on the fit channels --- a poor
    fit is the usual reason a kelvin axis should not be trusted.
    """

    temperature_k: np.ndarray
    tsys_k: float
    eta_mb: float
    baseline_rms: float

    @property
    def is_main_beam(self) -> bool:
        """True when a main-beam efficiency was applied (so the axis is ``T_B``)."""
        return self.eta_mb != 1.0

    @property
    def axis_label(self) -> str:
        """The honest y-axis label for this scale: ``T_B (K)`` or ``T_A (K)``."""
        return "T_B (K)" if self.is_main_beam else "T_A (K)"

    @property
    def peak_k(self) -> float:
        """The peak temperature (K), or 0.0 for an empty spectrum."""
        return float(self.temperature_k.max()) if self.temperature_k.size else 0.0


def brightness_temperature(
    freq_hz: np.ndarray,
    power_db: np.ndarray,
    *,
    tsys_k: float,
    exclude: tuple[float, float],
    eta_mb: float = 1.0,
    order: int = BASELINE_ORDER,
) -> TemperatureScale:
    """Put a relative-dB spectrum on a kelvin axis using a known system temperature.

    Parameters
    ----------
    freq_hz : numpy.ndarray
        Frequency axis in Hz.
    power_db : numpy.ndarray
        The averaged spectrum in dB (relative power), same shape as ``freq_hz``.
    tsys_k : float
        System temperature in kelvin, from an M10 sky/ground Y-factor calibration
        (:func:`jansky_observe.confirm.skyground.sky_ground_delta`).
    exclude : tuple of float
        ``(lo_hz, hi_hz)`` --- the HI Doppler window, excluded from the baseline fit so
        the line is not absorbed into the baseline it is measured against.
    eta_mb : float
        Main-beam efficiency. **Left at 1.0 the result is antenna temperature**, which
        is what :attr:`TemperatureScale.is_main_beam` reports.
    order : int
        Baseline polynomial order (default 3, the v1 classifier's).

    Returns
    -------
    TemperatureScale
        The kelvin spectrum plus the inputs and the baseline fit quality.

    Raises
    ------
    ValueError
        If ``tsys_k`` or ``eta_mb`` is not positive, if the axes' shapes differ, or
        (from :func:`~jansky_observe.confirm.baseline.fit_baseline`) if too few
        channels lie outside ``exclude`` to fit the polynomial.
    """
    if tsys_k <= 0:
        raise ValueError(f"tsys_k must be positive, got {tsys_k}")
    if eta_mb <= 0:
        raise ValueError(f"eta_mb must be positive, got {eta_mb}")

    freq = np.asarray(freq_hz, dtype=np.float64)
    db = np.asarray(power_db, dtype=np.float64)
    if freq.shape != db.shape:
        raise ValueError(f"freq/power shapes must match, got {freq.shape} and {db.shape}")

    linear = db_to_linear(db)
    fit = fit_baseline(freq, linear, exclude=exclude, order=order)

    # A baseline that dips to zero or below is unphysical for power and would blow the
    # ratio up; clamp to a small positive floor derived from the data itself.
    floor = float(np.abs(linear).max()) * 1e-12 or np.finfo(np.float64).tiny
    baseline = np.maximum(fit.baseline, floor)

    t_a = tsys_k * (linear / baseline - 1.0)
    return TemperatureScale(
        temperature_k=t_a / eta_mb,
        tsys_k=float(tsys_k),
        eta_mb=float(eta_mb),
        baseline_rms=float(fit.residual_rms),
    )
