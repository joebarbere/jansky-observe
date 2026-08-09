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

__all__ = ["dual_axis_plot", "rfi_spectrum_plot", "verdict_plot"]

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


def rfi_spectrum_plot(profile: object, out_path: str | Path) -> Path:
    """Render an RFI sweep: spectrum on top, **occupancy** underneath.

    Two panels because a sweep is two facts, and the second one is the one that gets thrown
    away. The top panel carries mean and peak power together: where they separate, the bin is
    episodic, and the gap between the two lines *is* the intermittency you would otherwise
    have to infer.

    The bottom panel is the fraction of sweeps in which each bin stood above the floor. A
    carrier reads as a full-height bar; a satellite pass or a radar reads as a short one at a
    frequency whose peak, in the panel above, may be 30 dB up.

    The protected 1400-1427 MHz band is shaded in both panels, because whether anything lives
    in there is the question the survey exists to answer.

    Duck-typed on the ``SweepProfile`` attributes rather than importing it, to keep the
    matplotlib import out of :mod:`jansky_observe.capture.rfi` — the same reason
    ``rfi_sweep_comparison`` avoids importing the ORM.
    """
    from jansky_observe.capture.rfi import ALLOCATIONS, PROTECTED_HI_BAND_HZ

    freq_mhz = np.asarray(profile.freq_hz) / 1e6  # type: ignore[attr-defined]
    mean_db = np.asarray(profile.mean_db)  # type: ignore[attr-defined]
    max_db = np.asarray(profile.max_db)  # type: ignore[attr-defined]
    occupancy = np.asarray(profile.occupancy)  # type: ignore[attr-defined]
    floor_db = float(profile.floor_db)  # type: ignore[attr-defined]
    threshold_db = float(profile.threshold_db)  # type: ignore[attr-defined]
    n_sweeps = int(profile.n_sweeps)  # type: ignore[attr-defined]

    fig, (top, bottom) = plt.subplots(
        2, 1, figsize=(10, 6), sharex=True, gridspec_kw={"height_ratios": [3, 1]}
    )

    lo_mhz, hi_mhz = PROTECTED_HI_BAND_HZ[0] / 1e6, PROTECTED_HI_BAND_HZ[1] / 1e6
    for axis in (top, bottom):
        axis.axvspan(lo_mhz, hi_mhz, color="tab:green", alpha=0.13, zorder=0)

    # Regional hints get a light marker and a label; only the protected band is shaded.
    for band in ALLOCATIONS:
        if band.authoritative:
            continue
        centre = (band.lo_hz + band.hi_hz) / 2e6
        if freq_mhz[0] <= centre <= freq_mhz[-1]:
            top.axvline(centre, color="grey", lw=0.5, ls=":", alpha=0.6, zorder=0)

    top.fill_between(
        freq_mhz, mean_db, max_db, color="tab:red", alpha=0.25, label="mean-to-peak spread"
    )
    top.plot(freq_mhz, max_db, lw=0.7, color="tab:red", label="peak")
    top.plot(freq_mhz, mean_db, lw=0.9, color="tab:blue", label="mean")
    top.axhline(floor_db, color="grey", lw=0.8, ls="--", label=f"floor {floor_db:.1f} dB")
    top.axhline(
        floor_db + threshold_db,
        color="grey",
        lw=0.6,
        ls=":",
        label=f"+{threshold_db:.0f} dB threshold",
    )
    top.set_ylabel("power (dB)")
    top.legend(loc="upper right", fontsize="x-small", ncol=2)
    top.set_title(
        f"RFI sweep — {n_sweeps} sweeps, {freq_mhz[0]:.0f}-{freq_mhz[-1]:.0f} MHz "
        f"(shaded: 1400-1427 MHz, ITU 5.340 — no emissions permitted)"
    )
    top.grid(alpha=0.25)

    bottom.bar(
        freq_mhz,
        occupancy * 100.0,
        width=(freq_mhz[1] - freq_mhz[0]) if len(freq_mhz) > 1 else 1.0,
        color="tab:purple",
        alpha=0.8,
    )
    bottom.set_ylim(0, 105)
    bottom.set_ylabel("occupancy (%)")
    bottom.set_xlabel("frequency (MHz)")
    bottom.grid(alpha=0.25)

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.tight_layout()
    fig.savefig(out, dpi=_DPI)
    plt.close(fig)
    return out
