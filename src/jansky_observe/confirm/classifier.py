"""The v1 rule-based HI spectrum classifier (plan §6, ``hline_v1``).

Deterministic, pure numpy — verdicts come only from this code, never from
an LLM (the provenance rule, plan §12.5): ``ClassifierResult`` rows cite
:data:`CLASSIFIER_NAME` + :data:`CLASSIFIER_VERSION`, and every number a
row wants lives in :attr:`ClassifierVerdict.params` (JSON-able).

Pipeline: dB → linear power → polynomial baseline fit *excluding* the
Doppler window → residual = power − baseline → peak = max residual inside
the window → SNR = peak / baseline residual RMS → verdict thresholds
``detected`` (SNR ≥ 5), ``uncertain`` (2–5), ``not_detected`` (< 2).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
from astropy.coordinates import SkyCoord

from jansky_observe.astro.lsr import HI_LINE_FREQ_HZ, doppler_window_hz
from jansky_observe.confirm.baseline import db_to_linear, fit_baseline, linear_to_db
from jansky_observe.confirm.onoff import difference_spectrum

__all__ = [
    "CLASSIFIER_NAME",
    "CLASSIFIER_ONOFF_NAME",
    "CLASSIFIER_VERSION",
    "ClassifierVerdict",
    "averaged_spectrum",
    "classify_capture_npz",
    "classify_difference_npz",
    "classify_spectrum",
    "running_classify",
]

CLASSIFIER_NAME = "hline_v1"
"""Classifier name cited by ClassifierResult rows (provenance, plan §12.5)."""

CLASSIFIER_ONOFF_NAME = "hline_v1_onoff"
"""Provenance name for a classify run on an ON−OFF *difference* spectrum
(roadmap M10). Distinct from :data:`CLASSIFIER_NAME` so ``ClassifierResult`` rows
are honest about the method; the classify logic itself is identical."""

CLASSIFIER_VERSION = "1"
"""Classifier version cited by ClassifierResult rows."""

FIXED_WINDOW_HALF_WIDTH_HZ = 1.2e6
"""Fallback Doppler half-window when no pointing/time is available (~253 km/s)."""

_BASELINE_ORDER = 3
_SNR_DETECTED = 5.0
_SNR_UNCERTAIN = 2.0


@dataclass(frozen=True)
class ClassifierVerdict:
    """One classifier run: verdict, score, and JSON-able parameters.

    ``params`` carries everything a ``ClassifierResult`` row wants:
    ``peak_freq_hz``, ``peak_snr``, ``baseline_rms``, ``baseline_order``,
    ``window_hz``, ``n_channels`` (plus ``window_source`` for capture
    runs) — all native Python types, so ``json.dumps(verdict.params)``
    always works.
    """

    verdict: str
    score: float
    name: str = CLASSIFIER_NAME
    version: str = CLASSIFIER_VERSION
    params: dict[str, Any] = field(default_factory=dict)


def _verdict_for_snr(snr: float) -> str:
    """Map SNR to the §6 verdict thresholds."""
    if snr >= _SNR_DETECTED:
        return "detected"
    if snr >= _SNR_UNCERTAIN:
        return "uncertain"
    return "not_detected"


def classify_spectrum(
    freq_hz: np.ndarray,
    power_db: np.ndarray,
    *,
    window_hz: tuple[float, float],
    name: str = CLASSIFIER_NAME,
) -> ClassifierVerdict:
    """Classify one averaged spectrum for an HI line inside a Doppler window.

    Parameters
    ----------
    freq_hz : numpy.ndarray
        Topocentric frequency axis in Hz.
    power_db : numpy.ndarray
        Averaged power spectrum in dB (same length as ``freq_hz``).
    window_hz : tuple of float
        ``(lo_hz, hi_hz)`` search window — from
        :func:`jansky_observe.astro.lsr.doppler_window_hz` when the
        pointing/time is known.
    name : str
        Classifier name recorded on the verdict.

    Returns
    -------
    ClassifierVerdict
        Verdict, score (= SNR), and JSON-able params.

    Raises
    ------
    ValueError
        If no channels fall inside the window, or too few fall outside it
        for the baseline fit.
    """
    freq = np.asarray(freq_hz, dtype=np.float64)
    linear = db_to_linear(np.asarray(power_db))
    lo_hz, hi_hz = float(window_hz[0]), float(window_hz[1])

    in_window = (freq >= lo_hz) & (freq <= hi_hz)
    if not bool(in_window.any()):
        raise ValueError(f"no channels inside window {lo_hz:.0f}–{hi_hz:.0f} Hz")

    fit = fit_baseline(freq, linear, exclude=(lo_hz, hi_hz), order=_BASELINE_ORDER)
    residual = linear - fit.baseline
    window_indices = np.flatnonzero(in_window)
    peak_index = int(window_indices[int(np.argmax(residual[window_indices]))])
    peak = float(residual[peak_index])
    snr = peak / fit.residual_rms if fit.residual_rms > 0.0 else float("inf")

    return ClassifierVerdict(
        verdict=_verdict_for_snr(snr),
        score=float(snr),
        name=name,
        version=CLASSIFIER_VERSION,
        params={
            "peak_freq_hz": float(freq[peak_index]),
            "peak_snr": float(snr),
            "baseline_rms": fit.residual_rms,
            "baseline_order": _BASELINE_ORDER,
            "window_hz": [lo_hz, hi_hz],
            "n_channels": int(freq.size),
        },
    )


def running_classify(
    freq_hz: np.ndarray,
    power_db_accumulated: np.ndarray,
    window_hz: tuple[float, float],
) -> ClassifierVerdict:
    """Classify the accumulating average — the live "am I seeing it?" badge.

    Identical to :func:`classify_spectrum`; the live view calls this on
    its running (linear-averaged) spectrum each update, and the badge
    shows the running SNR and verdict.
    """
    return classify_spectrum(freq_hz, power_db_accumulated, window_hz=window_hz)


def averaged_spectrum(path: str | Path) -> tuple[np.ndarray, np.ndarray]:
    """Load a ``.npz`` capture and return its averaged spectrum.

    Frames are averaged in LINEAR power (averaging dB would bias low),
    then converted back to dB. The frequency axis follows the fftshifted
    frame convention (:mod:`jansky_observe.frames`): index 0 is
    ``center_freq_hz - sample_rate_hz / 2``.

    Parameters
    ----------
    path : str or Path
        A ``.npz`` written by
        :class:`jansky_observe.capture.writer.NpzCaptureWriter`.

    Returns
    -------
    tuple of numpy.ndarray
        ``(freq_hz, power_db)`` — the topocentric frequency axis and the
        averaged spectrum in dB.
    """
    with np.load(Path(path), allow_pickle=False) as data:
        power_db = np.asarray(data["power_db"], dtype=np.float64)
        center_freq_hz = float(data["center_freq_hz"])
        sample_rate_hz = float(data["sample_rate_hz"])
    if power_db.ndim != 2:
        raise ValueError(f"expected power_db of shape (n, n_fft), got {power_db.shape}")
    n_fft = power_db.shape[1]
    freq_hz = center_freq_hz + (np.arange(n_fft) - n_fft / 2) * (sample_rate_hz / n_fft)
    avg_db = linear_to_db(db_to_linear(power_db).mean(axis=0))
    return freq_hz, avg_db


def classify_capture_npz(
    path: str | Path,
    *,
    lat_deg: float,
    lon_deg: float,
    elevation_m: float,
    coord: SkyCoord | None = None,
    when: datetime | None = None,
) -> ClassifierVerdict:
    """Classify a ``.npz`` capture end to end.

    The Doppler window comes from :func:`~jansky_observe.astro.lsr.doppler_window_hz`
    when both ``coord`` and ``when`` are given (|v_LSR| ≤ 250 km/s at that
    pointing/time); otherwise the fixed fallback window
    ``1420.406 MHz ± 1.2 MHz`` is used. ``params["window_source"]``
    records which (``"lsr"`` or ``"fixed"``).

    Parameters
    ----------
    path : str or Path
        A ``.npz`` written by
        :class:`jansky_observe.capture.writer.NpzCaptureWriter`.
    lat_deg, lon_deg, elevation_m : float
        Station location (geodetic degrees, metres).
    coord : SkyCoord, optional
        Pointing direction of the capture.
    when : datetime, optional
        UTC capture time.

    Returns
    -------
    ClassifierVerdict
        Verdict for the capture's averaged spectrum.
    """
    freq_hz, power_db = averaged_spectrum(path)
    if coord is not None and when is not None:
        window = doppler_window_hz(coord, lat_deg, lon_deg, elevation_m, when)
        window_source = "lsr"
    else:
        window = (
            HI_LINE_FREQ_HZ - FIXED_WINDOW_HALF_WIDTH_HZ,
            HI_LINE_FREQ_HZ + FIXED_WINDOW_HALF_WIDTH_HZ,
        )
        window_source = "fixed"
    verdict = classify_spectrum(freq_hz, power_db, window_hz=window)
    params = {**verdict.params, "window_source": window_source}
    json.dumps(params)  # provenance rows serialize params; fail loudly here if not
    return replace(verdict, params=params)


def classify_difference_npz(
    on_path: str | Path,
    off_path: str | Path,
    *,
    lat_deg: float,
    lon_deg: float,
    elevation_m: float,
    coord: SkyCoord | None = None,
    when: datetime | None = None,
    method: str = "ratio",
) -> ClassifierVerdict:
    """Classify the ON−OFF *difference* of two ``.npz`` captures end to end.

    Loads both captures' averaged spectra, differences them with
    :func:`jansky_observe.confirm.onoff.difference_spectrum`, then runs the same
    peak-in-Doppler-window classify as :func:`classify_capture_npz` on the
    result — but records the distinct provenance name
    :data:`CLASSIFIER_ONOFF_NAME`. The difference is already bandpass-flat (the
    whole point of an OFF), so the classifier's baseline fit becomes a near-no-op
    residual and the HI line stands alone.

    The Doppler window comes from
    :func:`~jansky_observe.astro.lsr.doppler_window_hz` when both ``coord`` and
    ``when`` are given, else the fixed fallback window;
    ``params["window_source"]`` records which. ``params["method"]`` records the
    difference method.

    Parameters
    ----------
    on_path, off_path : str or Path
        The ON and OFF ``.npz`` captures.
    lat_deg, lon_deg, elevation_m : float
        Station location (geodetic degrees, metres).
    coord : SkyCoord, optional
        Pointing direction of the ON capture.
    when : datetime, optional
        UTC capture time.
    method : str
        Difference method — ``"ratio"`` (default) or ``"subtract"``.

    Returns
    -------
    ClassifierVerdict
        Verdict for the difference spectrum, named :data:`CLASSIFIER_ONOFF_NAME`.

    Raises
    ------
    ValueError
        On an ON/OFF axis mismatch or an unknown ``method`` (from
        :func:`~jansky_observe.confirm.onoff.difference_spectrum`), or when no
        channels fall inside the Doppler window (from :func:`classify_spectrum`).
    """
    on_freq, on_db = averaged_spectrum(on_path)
    off_freq, off_db = averaged_spectrum(off_path)
    freq_hz, diff_db = difference_spectrum(on_freq, on_db, off_freq, off_db, method=method)
    if coord is not None and when is not None:
        window = doppler_window_hz(coord, lat_deg, lon_deg, elevation_m, when)
        window_source = "lsr"
    else:
        window = (
            HI_LINE_FREQ_HZ - FIXED_WINDOW_HALF_WIDTH_HZ,
            HI_LINE_FREQ_HZ + FIXED_WINDOW_HALF_WIDTH_HZ,
        )
        window_source = "fixed"
    verdict = classify_spectrum(freq_hz, diff_db, window_hz=window, name=CLASSIFIER_ONOFF_NAME)
    params = {**verdict.params, "window_source": window_source, "method": method}
    json.dumps(params)  # provenance rows serialize params; fail loudly here if not
    return replace(verdict, params=params)
