"""Sky/ground Y-factor reduction: ΔdB + Tsys (roadmap M10, plan M10 Piece 3).

The runbook's permanent "system-health" number (Milestone 7/9). Point the dish
at cold blank sky (``cold_sky``) and then at the warm ground (``hot_ground``) and
compare the total power in the band. The ratio of the two — the **Y-factor** —
gives both a trendable ΔdB and, with assumed physical temperatures, the receiver
system temperature ``Tsys``.

Pure numpy over the two averaged spectra (each a
:func:`jansky_observe.confirm.classifier.averaged_spectrum` in dB): the band-mean
linear power is ``db_to_linear(power_db).mean()``.

Assumed temperatures (defaults, both documented and citeable):

- ``t_hot_k = 300 K`` — the ground/ambient "hot load" the feed sees pointed down.
- ``t_cold_k = 10 K`` — cold blank sky (the note treats it as ~5–10 K).

The Y-factor relations::

    y        = mean(hot_lin) / mean(cold_lin)
    delta_db = 10 * log10(y)
    tsys_k   = (t_hot_k - y * t_cold_k) / (y - 1)

A real cold-sky/hot-ground pair always has ``y > 1`` (the ground is hotter than
the sky); ``y <= 1`` is unphysical and raises :class:`ValueError`.
"""

from __future__ import annotations

import numpy as np

from jansky_observe.confirm.baseline import db_to_linear

__all__ = ["sky_ground_delta"]


def sky_ground_delta(
    cold_db: np.ndarray,
    hot_db: np.ndarray,
    *,
    t_hot_k: float = 300.0,
    t_cold_k: float = 10.0,
) -> dict[str, float]:
    """Y-factor sky/ground reduction: band-mean ΔdB and system temperature.

    Parameters
    ----------
    cold_db, hot_db : numpy.ndarray
        The cold-sky and hot-ground averaged power spectra in dB (same shape).
    t_hot_k : float
        Assumed hot-load (ground/ambient) temperature in kelvin (default 300 K).
    t_cold_k : float
        Assumed cold-sky temperature in kelvin (default 10 K).

    Returns
    -------
    dict of str to float
        ``{"delta_db", "y", "tsys_k"}`` — the band-mean total-power ratio in dB,
        the linear Y-factor, and the Y-factor system temperature.

    Raises
    ------
    ValueError
        If the two spectra shapes differ, or if ``y <= 1`` (unphysical: the
        ground should be hotter than the cold sky, so ``mean(hot) > mean(cold)``).
    """
    cold = np.asarray(cold_db, dtype=np.float64)
    hot = np.asarray(hot_db, dtype=np.float64)
    if cold.shape != hot.shape:
        raise ValueError(
            f"cold/hot spectra shapes must match, got cold={cold.shape}, hot={hot.shape}"
        )

    cold_lin = db_to_linear(cold).mean()
    hot_lin = db_to_linear(hot).mean()
    y = float(hot_lin / cold_lin)
    if y <= 1.0:
        raise ValueError(
            f"unphysical Y-factor y={y:.4f} <= 1: the hot-ground band power must exceed "
            "the cold-sky band power (check the cold_sky/hot_ground captures are not swapped)"
        )

    delta_db = float(10.0 * np.log10(y))
    tsys_k = float((t_hot_k - y * t_cold_k) / (y - 1.0))
    return {"delta_db": delta_db, "y": y, "tsys_k": tsys_k}
