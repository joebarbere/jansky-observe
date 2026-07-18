"""Radiometer-equation sensitivity (roadmap M12) — an estimate, not a verdict.

Given the system temperature (from an M10 sky/ground calibration), the per-channel
bandwidth, and the integration time, the radiometer equation gives the theoretical
noise floor of a spectrum::

    ΔT_rms = T_sys / sqrt(Δν_channel · τ)

From that we can state the *predicted* SNR of a line of assumed brightness and the
integration time needed to reach a target SNR — which turns a non-detection into an
honest "under-integrated vs. genuinely absent" call. This is an advisory estimate
that rides alongside the classifier's empirical SNR; it is **never** a detection
verdict (verdicts come only from the deterministic classifiers, plan §12.5).
"""

from __future__ import annotations

import math
from typing import Any

__all__ = ["radiometer_estimate"]


def radiometer_estimate(
    *,
    tsys_k: float,
    channel_bw_hz: float,
    integration_s: float,
    target_snr: float = 5.0,
    assumed_line_k: float = 50.0,
    measured_peak_k: float | None = None,
) -> dict[str, Any]:
    """The radiometer noise floor + predicted SNR + time-to-detect (an estimate).

    Parameters
    ----------
    tsys_k : float
        System temperature in kelvin (from an M10 sky/ground Y-factor calibration).
    channel_bw_hz : float
        Per-channel bandwidth in Hz (``sample_rate_hz / n_fft``).
    integration_s : float
        Total on-source integration time in seconds.
    target_snr : float
        The SNR the time-to-detect is computed for (default 5, the classifier's
        ``detected`` threshold).
    assumed_line_k : float
        Assumed HI line brightness in kelvin for the predicted SNR / time-to-detect
        (galactic-plane HI ≈ 30–100 K; default 50). Derive from a fetched reference
        profile's peak when one is available (roadmap M12 Piece 2).
    measured_peak_k : float, optional
        A measured line peak in kelvin, if absolute calibration is available; yields
        an ``achieved_snr``. ``None`` while spectra are relative (the usual case) —
        use the classifier's empirical SNR for the achieved figure instead.

    Returns
    -------
    dict
        JSON-able: ``delta_t_rms_k``, ``delta_t_rms_mk``, ``predicted_snr``,
        ``time_to_target_s``, ``achieved_snr`` (or ``None``), plus the inputs.

    Raises
    ------
    ValueError
        If ``tsys_k``, ``channel_bw_hz``, ``integration_s``, or ``assumed_line_k``
        is not positive.
    """
    if tsys_k <= 0 or channel_bw_hz <= 0 or integration_s <= 0 or assumed_line_k <= 0:
        raise ValueError(
            "tsys_k, channel_bw_hz, integration_s, assumed_line_k must all be positive "
            f"(got {tsys_k}, {channel_bw_hz}, {integration_s}, {assumed_line_k})"
        )
    delta_t_rms_k = tsys_k / math.sqrt(channel_bw_hz * integration_s)
    predicted_snr = assumed_line_k / delta_t_rms_k
    # τ to reach target_snr on an assumed_line_k line: from ΔT_rms = line/target_snr.
    time_to_target_s = (tsys_k * target_snr / assumed_line_k) ** 2 / channel_bw_hz
    achieved_snr = measured_peak_k / delta_t_rms_k if measured_peak_k is not None else None
    return {
        "delta_t_rms_k": delta_t_rms_k,
        "delta_t_rms_mk": delta_t_rms_k * 1e3,
        "predicted_snr": predicted_snr,
        "time_to_target_s": time_to_target_s,
        "achieved_snr": achieved_snr,
        "tsys_k": tsys_k,
        "channel_bw_hz": channel_bw_hz,
        "integration_s": integration_s,
        "target_snr": target_snr,
        "assumed_line_k": assumed_line_k,
    }
