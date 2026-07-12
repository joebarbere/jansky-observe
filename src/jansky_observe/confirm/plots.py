"""Verdict and dual-axis plots for classifier results (plan §6, §4.6).

Headless-safe: the Agg backend is selected before pyplot is imported, so
these render identically on the Pi, in CI, and in tests — no display, no
window system.
"""

from __future__ import annotations

import matplotlib

matplotlib.use("Agg")

from pathlib import Path  # noqa: E402

import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import numpy.typing as npt  # noqa: E402

from jansky_observe.confirm.baseline import db_to_linear, fit_baseline, linear_to_db  # noqa: E402
from jansky_observe.confirm.classifier import ClassifierVerdict  # noqa: E402

__all__ = ["dual_axis_plot", "verdict_plot"]

_DPI = 120
_WINDOW_COLOR = "tab:orange"
_VERDICT_COLORS = {"detected": "tab:green", "uncertain": "tab:orange", "not_detected": "tab:red"}


def verdict_plot(
    freq_hz: np.ndarray,
    power_db: np.ndarray,
    verdict: ClassifierVerdict,
    out_path: str | Path,
) -> Path:
    """Render the classifier's view of a spectrum to a PNG.

    One figure: the spectrum in dB, the fitted baseline (refit from the
    verdict's recorded window and order — the same deterministic code path
    the classifier ran), the shaded Doppler window, the peak marker, and a
    title carrying verdict + SNR + classifier name/version.

    Parameters
    ----------
    freq_hz : numpy.ndarray
        Topocentric frequency axis in Hz.
    power_db : numpy.ndarray
        The classified spectrum in dB.
    verdict : ClassifierVerdict
        The classifier output for this spectrum.
    out_path : str or Path
        Destination PNG path.

    Returns
    -------
    Path
        The written file's path.
    """
    freq = np.asarray(freq_hz, dtype=np.float64)
    freq_mhz = freq / 1e6
    lo_hz, hi_hz = (float(v) for v in verdict.params["window_hz"])
    order = int(verdict.params["baseline_order"])
    peak_freq_hz = float(verdict.params["peak_freq_hz"])

    fit = fit_baseline(freq, db_to_linear(np.asarray(power_db)), (lo_hz, hi_hz), order=order)
    baseline_db = linear_to_db(fit.baseline)
    peak_index = int(np.argmin(np.abs(freq - peak_freq_hz)))

    fig, ax = plt.subplots(figsize=(9, 5))
    ax.plot(freq_mhz, power_db, color="tab:blue", lw=0.9, label="spectrum")
    ax.plot(freq_mhz, baseline_db, color="tab:gray", lw=1.2, ls="--", label="baseline")
    ax.axvspan(lo_hz / 1e6, hi_hz / 1e6, color=_WINDOW_COLOR, alpha=0.15, label="Doppler window")
    ax.plot(
        freq_mhz[peak_index],
        np.asarray(power_db)[peak_index],
        marker="v",
        color=_VERDICT_COLORS.get(verdict.verdict, "black"),
        ms=10,
        ls="none",
        label="peak",
    )
    ax.set_xlabel("Topocentric frequency (MHz)")
    ax.set_ylabel("Power (dB)")
    ax.set_title(f"{verdict.verdict} — SNR {verdict.score:.1f} ({verdict.name} v{verdict.version})")
    ax.legend(loc="upper right", fontsize=8)
    fig.tight_layout()

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=_DPI)
    plt.close(fig)
    return out


def dual_axis_plot(
    freq_hz: np.ndarray,
    power_db: np.ndarray,
    vlsr_kms: np.ndarray,
    out_path: str | Path,
) -> Path:
    """Render a spectrum with both frequency and v_LSR axes (plan §4.6).

    The primary x-axis is topocentric MHz; the secondary (top) x-axis is
    v_LSR in km/s, mapped through the supplied per-channel velocities
    (from :func:`jansky_observe.astro.lsr.vlsr_axis`) — the §4.6 promise
    that every spectrum renders both axes.

    Parameters
    ----------
    freq_hz : numpy.ndarray
        Topocentric frequency axis in Hz.
    power_db : numpy.ndarray
        Spectrum in dB.
    vlsr_kms : numpy.ndarray
        v_LSR per channel in km/s (same length as ``freq_hz``).
    out_path : str or Path
        Destination PNG path.

    Returns
    -------
    Path
        The written file's path.
    """
    freq_mhz = np.asarray(freq_hz, dtype=np.float64) / 1e6
    velocity = np.asarray(vlsr_kms, dtype=np.float64)
    # v_LSR decreases with frequency; np.interp needs increasing x.
    freq_by_v = np.argsort(velocity)
    v_by_freq = np.argsort(freq_mhz)

    def mhz_to_kms(mhz: npt.ArrayLike) -> np.ndarray:
        x = np.asarray(mhz, dtype=np.float64)
        return np.interp(x, freq_mhz[v_by_freq], velocity[v_by_freq])

    def kms_to_mhz(kms: npt.ArrayLike) -> np.ndarray:
        x = np.asarray(kms, dtype=np.float64)
        return np.interp(x, velocity[freq_by_v], freq_mhz[freq_by_v])

    fig, ax = plt.subplots(figsize=(9, 5))
    ax.plot(freq_mhz, power_db, color="tab:blue", lw=0.9)
    ax.set_xlabel("Topocentric frequency (MHz)")
    ax.set_ylabel("Power (dB)")
    secax = ax.secondary_xaxis("top", functions=(mhz_to_kms, kms_to_mhz))
    secax.set_xlabel("v$_{LSR}$ (km/s)")
    fig.tight_layout()

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=_DPI)
    plt.close(fig)
    return out
