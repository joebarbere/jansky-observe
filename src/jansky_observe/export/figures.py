"""Report figures: the integrated profile and the waterfall (plan §7, §4.6).

Headless-safe: the Agg backend is selected before pyplot is imported, same
as :mod:`jansky_observe.confirm.plots`, so these render identically on the
Pi, in CI, and in tests. Every figure is closed after saving.
"""

from __future__ import annotations

import matplotlib

matplotlib.use("Agg")

from datetime import datetime  # noqa: E402
from pathlib import Path  # noqa: E402

import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from astropy.coordinates import SkyCoord  # noqa: E402

from jansky_observe.astro.lsr import vlsr_axis  # noqa: E402
from jansky_observe.confirm.classifier import averaged_spectrum  # noqa: E402
from jansky_observe.confirm.plots import dual_axis_plot  # noqa: E402

__all__ = ["profile_figure", "waterfall_figure"]

_DPI = 120


def profile_figure(
    npz_path: str | Path,
    out_png: str | Path,
    *,
    coord: SkyCoord | None = None,
    lat: float | None = None,
    lon: float | None = None,
    elev: float | None = None,
    when: datetime | None = None,
) -> Path:
    """Render a capture's integrated (linear-averaged) spectrum to a PNG.

    With full pointing information the plot carries the dual MHz / v_LSR
    axes (plan §4.6) via :func:`jansky_observe.confirm.plots.dual_axis_plot`;
    without it, a single topocentric-MHz axis in the same style.

    Parameters
    ----------
    npz_path : str or Path
        A ``.npz`` written by
        :class:`jansky_observe.capture.writer.NpzCaptureWriter`.
    out_png : str or Path
        Destination PNG path (parent directories are created).
    coord : SkyCoord, optional
        Pointing direction of the capture.
    lat, lon, elev : float, optional
        Station location (geodetic degrees, metres).
    when : datetime, optional
        UTC capture time.

    Returns
    -------
    Path
        The written file's path.
    """
    freq_hz, power_db = averaged_spectrum(npz_path)
    have_pointing = (
        coord is not None
        and lat is not None
        and lon is not None
        and elev is not None
        and when is not None
    )
    if have_pointing:
        assert coord is not None and lat is not None and lon is not None  # narrow for mypy
        assert elev is not None and when is not None
        velocity = vlsr_axis(freq_hz, coord, lat, lon, elev, when)
        return dual_axis_plot(freq_hz, power_db, velocity, out_png)

    fig, ax = plt.subplots(figsize=(9, 5))
    ax.plot(freq_hz / 1e6, power_db, color="tab:blue", lw=0.9)
    ax.set_xlabel("Topocentric frequency (MHz)")
    ax.set_ylabel("Power (dB)")
    fig.tight_layout()
    out = Path(out_png)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=_DPI)
    plt.close(fig)
    return out


def waterfall_figure(npz_path: str | Path, out_png: str | Path) -> Path:
    """Render a capture's spectral rows as a waterfall PNG.

    ``power_db`` rows are shown with :func:`~matplotlib.axes.Axes.imshow`
    (viridis): frequency in MHz on x, elapsed time on y — seconds since the
    first frame, from the recorded per-frame timestamps, increasing
    downward (newest row at the bottom, matching the live view).

    Parameters
    ----------
    npz_path : str or Path
        A ``.npz`` written by
        :class:`jansky_observe.capture.writer.NpzCaptureWriter`.
    out_png : str or Path
        Destination PNG path (parent directories are created).

    Returns
    -------
    Path
        The written file's path.
    """
    with np.load(Path(npz_path), allow_pickle=False) as data:
        power_db = np.asarray(data["power_db"], dtype=np.float64)
        timestamps = np.asarray(data["timestamps"], dtype=np.float64)
        center_freq_hz = float(data["center_freq_hz"])
        sample_rate_hz = float(data["sample_rate_hz"])
    if power_db.ndim != 2:
        raise ValueError(f"expected power_db of shape (n, n_fft), got {power_db.shape}")
    n_fft = power_db.shape[1]
    freq_mhz = (center_freq_hz + (np.arange(n_fft) - n_fft / 2) * (sample_rate_hz / n_fft)) / 1e6
    elapsed_s = float(timestamps[-1] - timestamps[0]) if timestamps.size > 1 else 1.0

    fig, ax = plt.subplots(figsize=(9, 5))
    image = ax.imshow(
        power_db,
        aspect="auto",
        cmap="viridis",
        origin="upper",
        extent=(float(freq_mhz[0]), float(freq_mhz[-1]), elapsed_s, 0.0),
        interpolation="nearest",
    )
    ax.set_xlabel("Topocentric frequency (MHz)")
    ax.set_ylabel("Elapsed time (s)")
    fig.colorbar(image, ax=ax, label="Power (dB)")
    fig.tight_layout()

    out = Path(out_png)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=_DPI)
    plt.close(fig)
    return out
