"""Observed-vs-reference-model comparison in kelvin (plans/calibrated-overlay.md).

Once :mod:`jansky_observe.confirm.tbscale` has put the observed spectrum on a kelvin
axis, the comparison against a reference profile stops being an impression and becomes
three numbers:

* **``scale_ratio``** --- least squares ``observed ~= a * model`` through the origin.
  This is the headline, and it is the one a shape-only overlay cannot produce. An
  uncorrected main-beam efficiency shows up here as a constant well below 1 (0.5-0.7 for
  a small dish with a simple feed) while the profile shape still matches beautifully.
* ``residual_rms_k`` --- how much is left after the model is subtracted.
* ``peak_dv_kms`` --- observed peak velocity minus model peak velocity. A value that
  grows across sessions is a frequency-calibration alarm, which is what
  ``/compare-observations`` already watches for.

**The ratio is reported, never applied.** Rescaling the observed trace to sit on the
model would hide exactly the defect this module exists to surface.

Pure numpy, no IO. Advisory analysis, never a verdict (plan section 12.5).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

__all__ = ["MIN_OVERLAP_CHANNELS", "ModelComparison", "compare_to_model"]

#: Fewer overlapping channels than this and no comparison is reported --- a scale ratio
#: fitted to a handful of points is noise wearing a number's clothes.
MIN_OVERLAP_CHANNELS = 8


@dataclass(frozen=True)
class ModelComparison:
    """Observed vs reference model over their common velocity range, in kelvin."""

    scale_ratio: float
    residual_rms_k: float
    peak_dv_kms: float
    observed_peak_k: float
    model_peak_k: float
    overlap_kms: tuple[float, float]
    n_channels: int
    v_lsr_kms: np.ndarray
    observed_k: np.ndarray
    model_k: np.ndarray
    residual_k: np.ndarray

    def stats(self) -> dict[str, Any]:
        """The JSON-able scalar summary (no arrays)."""
        return {
            "scale_ratio": self.scale_ratio,
            "residual_rms_k": self.residual_rms_k,
            "peak_dv_kms": self.peak_dv_kms,
            "observed_peak_k": self.observed_peak_k,
            "model_peak_k": self.model_peak_k,
            "overlap_kms": list(self.overlap_kms),
            "n_channels": self.n_channels,
        }


def compare_to_model(
    v_obs_kms: np.ndarray,
    observed_k: np.ndarray,
    v_model_kms: np.ndarray,
    model_k: np.ndarray,
) -> ModelComparison | None:
    """Compare an observed kelvin spectrum against a reference profile.

    The model is interpolated onto the observed velocity grid over the two axes' common
    range; everything is computed there, so a partial-overlap comparison reports the
    range it actually used rather than quietly padding.

    Parameters
    ----------
    v_obs_kms, observed_k : numpy.ndarray
        The observed spectrum's v_LSR axis (km/s) and temperature (K), same shape.
    v_model_kms, model_k : numpy.ndarray
        The reference profile's velocity axis (km/s) and brightness temperature (K),
        same shape as each other.

    Returns
    -------
    ModelComparison or None
        ``None`` when the axes overlap in fewer than :data:`MIN_OVERLAP_CHANNELS`
        observed channels, or when the model is identically zero over the overlap (no
        scale ratio is defined against a zero model).

    Raises
    ------
    ValueError
        If either pair of arrays has mismatched shapes.
    """
    v_obs = np.asarray(v_obs_kms, dtype=np.float64)
    obs = np.asarray(observed_k, dtype=np.float64)
    v_mod = np.asarray(v_model_kms, dtype=np.float64)
    mod = np.asarray(model_k, dtype=np.float64)
    if v_obs.shape != obs.shape:
        raise ValueError(f"observed axis/values shapes differ: {v_obs.shape} vs {obs.shape}")
    if v_mod.shape != mod.shape:
        raise ValueError(f"model axis/values shapes differ: {v_mod.shape} vs {mod.shape}")
    if v_obs.size == 0 or v_mod.size == 0:
        return None

    lo = max(float(v_obs.min()), float(v_mod.min()))
    hi = min(float(v_obs.max()), float(v_mod.max()))
    if not hi > lo:
        return None

    inside = (v_obs >= lo) & (v_obs <= hi)
    if int(inside.sum()) < MIN_OVERLAP_CHANNELS:
        return None

    v = v_obs[inside]
    o = obs[inside]

    # np.interp needs increasing x; the model axis may run either way.
    order = np.argsort(v_mod)
    m = np.interp(v, v_mod[order], mod[order])

    denom = float(m @ m)
    if denom <= 0.0:
        return None  # a zero model admits no scale ratio

    residual = o - m
    return ModelComparison(
        scale_ratio=float((o @ m) / denom),
        residual_rms_k=float(np.sqrt(np.mean(residual**2))),
        peak_dv_kms=float(v[int(np.argmax(o))] - v[int(np.argmax(m))]),
        observed_peak_k=float(o.max()),
        model_peak_k=float(m.max()),
        overlap_kms=(lo, hi),
        n_channels=int(v.size),
        v_lsr_kms=v,
        observed_k=o,
        model_k=m,
        residual_k=residual,
    )
