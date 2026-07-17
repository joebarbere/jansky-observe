"""Sky-map reduction: pointed spectra → a gridded 2-D map (roadmap M11).

Two pure-numpy steps, deliberately independent of how the captures were acquired
(a rotator raster or ingested drift passes), so both are synthetic-testable and
usable before any drive exists:

* :func:`cell_value` reduces one capture's averaged spectrum to a single scalar —
  HI line intensity, peak v_LSR, or band total power.
* :func:`grid_map` bins those scalars onto a regular grid with **beam-weighted**
  (Gaussian-of-HPBW) accumulation, leaving cells with no nearby sample as gaps
  (NaN) rather than inventing values — the honest treatment for a ~21° beam.

The map is beam-limited: each grid pixel is a beam-smoothed average, and nothing
smaller than the HPBW is resolvable. Callers must render that caveat (the figure
draws an HPBW circle); the data here never implies more resolution than the dish
has.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from jansky_observe.astro.lsr import HI_LINE_FREQ_HZ
from jansky_observe.confirm.baseline import db_to_linear, fit_baseline, linear_to_db

__all__ = ["GriddedMap", "cell_value", "grid_map"]

_C_KM_PER_S = 299_792.458
_BASELINE_ORDER = 3
_FWHM_TO_SIGMA = 1.0 / 2.354_820_045  # 2*sqrt(2*ln2)


def _peak_index_in_window(freq: np.ndarray, linear: np.ndarray, lo_hz: float, hi_hz: float) -> int:
    """Index of the max baseline-subtracted residual inside the Doppler window.

    Same reduction the ``hline_v1`` classifier uses: a polynomial baseline is fit
    *excluding* the window, and the peak is the largest residual within it.
    """
    in_window = (freq >= lo_hz) & (freq <= hi_hz)
    if not bool(in_window.any()):
        raise ValueError(f"no channels inside window {lo_hz:.0f}–{hi_hz:.0f} Hz")
    fit = fit_baseline(freq, linear, exclude=(lo_hz, hi_hz), order=_BASELINE_ORDER)
    residual = linear - fit.baseline
    window_indices = np.flatnonzero(in_window)
    return int(window_indices[int(np.argmax(residual[window_indices]))])


def cell_value(
    freq_hz: np.ndarray,
    power_db: np.ndarray,
    *,
    metric: str,
    window_hz: tuple[float, float],
    vlsr: np.ndarray | None = None,
) -> float:
    """Reduce one averaged spectrum to a single map-cell scalar.

    Parameters
    ----------
    freq_hz : numpy.ndarray
        Topocentric frequency axis in Hz.
    power_db : numpy.ndarray
        Averaged power spectrum in dB (same length as ``freq_hz``).
    metric : str
        One of :data:`~jansky_observe.models.SKY_MAP_METRICS`:

        * ``"hi_intensity"`` — baseline-subtracted line flux integrated over the
          Doppler window (arb; ∝ K·km/s). May be negative on a noise-only cell.
        * ``"peak_vlsr"`` — v_LSR (km/s) of the in-window peak. Uses ``vlsr`` when
          given (proper LSR axis); otherwise a topocentric radio velocity vs the
          HI rest frequency.
        * ``"total_power"`` — band-mean power in dB (continuum: Sun, plane).
    window_hz : tuple of float
        ``(lo_hz, hi_hz)`` Doppler search window.
    vlsr : numpy.ndarray, optional
        v_LSR (km/s) per channel, aligned to ``freq_hz`` — for ``"peak_vlsr"``.

    Returns
    -------
    float
        The reduced scalar.

    Raises
    ------
    ValueError
        Unknown ``metric``, or (for the line metrics) no channels inside the
        window / too few outside it for the baseline fit.
    """
    freq = np.asarray(freq_hz, dtype=np.float64)
    linear = db_to_linear(np.asarray(power_db))

    if metric == "total_power":
        return float(linear_to_db(np.asarray(linear.mean(), dtype=np.float64)))

    lo_hz, hi_hz = float(window_hz[0]), float(window_hz[1])
    if metric == "hi_intensity":
        in_window = (freq >= lo_hz) & (freq <= hi_hz)
        if not bool(in_window.any()):
            raise ValueError(f"no channels inside window {lo_hz:.0f}–{hi_hz:.0f} Hz")
        fit = fit_baseline(freq, linear, exclude=(lo_hz, hi_hz), order=_BASELINE_ORDER)
        residual = linear - fit.baseline
        idx = np.flatnonzero(in_window)
        # Integrate the residual over the window in frequency (trapezoid). The
        # sign is honest: a noise-only cell integrates near zero or negative.
        return float(np.trapezoid(residual[idx], freq[idx]))

    if metric == "peak_vlsr":
        peak = _peak_index_in_window(freq, linear, lo_hz, hi_hz)
        if vlsr is not None:
            return float(np.asarray(vlsr, dtype=np.float64)[peak])
        # No LSR axis: topocentric radio velocity against the HI rest frequency.
        return float(_C_KM_PER_S * (HI_LINE_FREQ_HZ - freq[peak]) / HI_LINE_FREQ_HZ)

    raise ValueError(f"unknown metric {metric!r} (expected one of SKY_MAP_METRICS)")


@dataclass(frozen=True)
class GriddedMap:
    """A gridded sky map.

    ``grid`` is ``(n_y, n_x)`` with **NaN** in cells that no sample covered
    (unobserved — never interpolated across). ``x_axis`` / ``y_axis`` are the
    cell-centre coordinates (degrees, in the map's frame); ``observed`` is the
    boolean coverage mask; ``n_samples`` is how many captures fed the map.
    """

    grid: np.ndarray
    x_axis: np.ndarray
    y_axis: np.ndarray
    observed: np.ndarray
    n_samples: int


def _axis(center_deg: float, extent_deg: float, step_deg: float) -> np.ndarray:
    """Cell-centre coordinates spanning ``extent`` around ``center`` at ``step``."""
    n = max(1, int(round(extent_deg / step_deg)) + 1)
    return center_deg + (np.arange(n) - (n - 1) / 2.0) * step_deg


def grid_map(
    x_deg: np.ndarray,
    y_deg: np.ndarray,
    values: np.ndarray,
    *,
    center_x_deg: float,
    center_y_deg: float,
    extent_x_deg: float,
    extent_y_deg: float,
    step_deg: float,
    hpbw_deg: float,
    max_gap_deg: float | None = None,
) -> GriddedMap:
    """Bin per-sample scalars onto a regular grid, beam-weighted.

    Each grid cell is the Gaussian-of-HPBW weighted mean of the samples
    (``weight = exp(-½(d/σ)²)``, ``σ`` from the beam FWHM) — the honest
    interpolant for a beam-smoothed instrument. A cell whose nearest sample is
    farther than ``max_gap_deg`` (default: the HPBW) is **unobserved** and comes
    back NaN; gaps are never filled by extrapolation.

    Parameters
    ----------
    x_deg, y_deg : numpy.ndarray
        Per-sample coordinates in the map frame (degrees).
    values : numpy.ndarray
        Per-sample reduced scalars (from :func:`cell_value`). Non-finite samples
        are dropped.
    center_x_deg, center_y_deg, extent_x_deg, extent_y_deg, step_deg : float
        Grid geometry (degrees) in the map frame.
    hpbw_deg : float
        Beam half-power width — sets the Gaussian kernel width.
    max_gap_deg : float, optional
        Coverage radius; a cell with no sample within it is left NaN. Defaults to
        ``hpbw_deg``.

    Returns
    -------
    GriddedMap
        The gridded map, axes, coverage mask, and sample count.
    """
    x = np.asarray(x_deg, dtype=np.float64)
    y = np.asarray(y_deg, dtype=np.float64)
    v = np.asarray(values, dtype=np.float64)
    finite = np.isfinite(x) & np.isfinite(y) & np.isfinite(v)
    x, y, v = x[finite], y[finite], v[finite]

    x_axis = _axis(center_x_deg, extent_x_deg, step_deg)
    y_axis = _axis(center_y_deg, extent_y_deg, step_deg)
    grid = np.full((y_axis.size, x_axis.size), np.nan, dtype=np.float64)
    observed = np.zeros((y_axis.size, x_axis.size), dtype=bool)
    if v.size == 0:
        return GriddedMap(grid, x_axis, y_axis, observed, 0)

    sigma = max(hpbw_deg * _FWHM_TO_SIGMA, 1e-6)
    gap = hpbw_deg if max_gap_deg is None else max_gap_deg
    gx, gy = np.meshgrid(x_axis, y_axis)  # (n_y, n_x)
    cells_x = gx.ravel()[:, None]  # (C, 1)
    cells_y = gy.ravel()[:, None]
    dist2 = (x[None, :] - cells_x) ** 2 + (y[None, :] - cells_y) ** 2  # (C, S)
    weights = np.exp(-0.5 * dist2 / sigma**2)
    wsum = weights.sum(axis=1)
    vnum = (weights * v[None, :]).sum(axis=1)
    dmin = np.sqrt(dist2.min(axis=1))
    covered = (dmin <= gap) & (wsum > 0.0)
    flat = np.full(cells_x.shape[0], np.nan, dtype=np.float64)
    flat[covered] = vnum[covered] / wsum[covered]
    grid = flat.reshape(gx.shape)
    observed = covered.reshape(gx.shape)
    return GriddedMap(grid, x_axis, y_axis, observed, int(v.size))
