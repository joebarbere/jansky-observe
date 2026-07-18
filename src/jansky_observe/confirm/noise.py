"""Total-power noise diagnostic (roadmap M12): is the noise Gaussian?

Clean thermal noise, averaged across a spectrum's channels, is very nearly
Gaussian frame-to-frame (central limit over the channels). A skewed or
heavy-tailed distribution of per-frame total power is a tell for RFI bursts or
ADC saturation. This reduces a capture's frames to that distribution + a
Gaussian fit + a non-Gaussianity flag — a diagnostic, not a verdict.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
from scipy import stats

from jansky_observe.confirm.baseline import db_to_linear

__all__ = ["PowerDistribution", "power_distribution"]

#: |skew| or |excess kurtosis| beyond this flags the distribution non-Gaussian.
_NONGAUSSIAN_THRESHOLD = 1.0
_MIN_FRAMES = 3


@dataclass(frozen=True)
class PowerDistribution:
    """Per-frame total-power distribution + a Gaussian fit.

    ``series`` is the per-frame band-mean linear power (one value per frame);
    ``mean``/``sigma`` are its Gaussian fit; ``skew``/``excess_kurtosis`` measure
    departure from Gaussian; ``non_gaussian`` is the RFI/saturation flag.
    """

    series: np.ndarray
    n_frames: int
    mean: float
    sigma: float
    skew: float
    excess_kurtosis: float
    non_gaussian: bool

    def stats(self) -> dict[str, Any]:
        """JSON-able summary (drops the raw series)."""
        return {
            "n_frames": self.n_frames,
            "mean": self.mean,
            "sigma": self.sigma,
            "skew": self.skew,
            "excess_kurtosis": self.excess_kurtosis,
            "non_gaussian": self.non_gaussian,
        }


def power_distribution(power_db: np.ndarray) -> PowerDistribution:
    """Reduce a capture's spectral frames to their total-power distribution.

    Parameters
    ----------
    power_db : numpy.ndarray
        The ``(n_frames, n_fft)`` per-frame power spectrum in dB (a capture's
        ``power_db`` array). Each frame is reduced to its band-mean LINEAR power.

    Returns
    -------
    PowerDistribution
        The per-frame series, its Gaussian fit, and the non-Gaussianity flag.

    Raises
    ------
    ValueError
        If ``power_db`` is not 2-D or has fewer than three frames.
    """
    arr = np.asarray(power_db, dtype=np.float64)
    if arr.ndim != 2:
        raise ValueError(f"expected power_db of shape (n_frames, n_fft), got {arr.shape}")
    if arr.shape[0] < _MIN_FRAMES:
        raise ValueError(f"need at least {_MIN_FRAMES} frames, got {arr.shape[0]}")
    series = db_to_linear(arr).mean(axis=1)
    sigma = float(series.std())
    if sigma == 0.0:
        # A perfectly flat series has undefined moments — treat as trivially Gaussian.
        skew = 0.0
        excess_kurtosis = 0.0
    else:
        skew = float(stats.skew(series))
        excess_kurtosis = float(stats.kurtosis(series))  # Fisher: 0 == Gaussian
    non_gaussian = (
        abs(skew) > _NONGAUSSIAN_THRESHOLD or abs(excess_kurtosis) > _NONGAUSSIAN_THRESHOLD
    )
    return PowerDistribution(
        series=series,
        n_frames=int(series.size),
        mean=float(series.mean()),
        sigma=sigma,
        skew=skew,
        excess_kurtosis=excess_kurtosis,
        non_gaussian=bool(non_gaussian),
    )
