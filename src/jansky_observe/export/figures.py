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
from matplotlib.patches import Circle  # noqa: E402

from jansky_observe.astro.lsr import vlsr_axis  # noqa: E402
from jansky_observe.confirm.classifier import averaged_spectrum  # noqa: E402
from jansky_observe.confirm.mapping import GriddedMap  # noqa: E402
from jansky_observe.confirm.plots import dual_axis_plot  # noqa: E402

__all__ = ["profile_figure", "sky_map_figure", "waterfall_figure"]

_DPI = 120

#: Per-frame axis labels for the sky-map heatmap.
_FRAME_LABELS: dict[str, tuple[str, str]] = {
    "galactic": ("Galactic longitude l (°)", "Galactic latitude b (°)"),
    "azel": ("Azimuth (°)", "Elevation (°)"),
}
#: Per-metric colorbar labels and colormaps (diverging for a velocity field).
_METRIC_STYLE: dict[str, tuple[str, str]] = {
    "hi_intensity": ("HI line intensity (arb ∝ K·km/s)", "viridis"),
    "peak_vlsr": ("Peak v_LSR (km/s)", "RdBu_r"),
    "total_power": ("Band total power (dB)", "magma"),
}


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


def sky_map_figure(
    gridded: GriddedMap,
    out_png: str | Path,
    *,
    frame: str,
    metric: str,
    hpbw_deg: float,
    title: str | None = None,
) -> Path:
    """Render a gridded sky map to a heatmap PNG (roadmap M11).

    The map is drawn with :func:`~matplotlib.axes.Axes.imshow`; **unobserved
    cells (NaN) are left transparent over a hatched background** so gaps read as
    gaps, never as data. A beam (HPBW) circle and a resolution caption are drawn
    unconditionally — the map is beam-limited and must say so; a viewer must not
    mistake a pixel for resolved structure.

    Parameters
    ----------
    gridded : GriddedMap
        The gridded map from :func:`jansky_observe.confirm.mapping.grid_map`.
    out_png : str or Path
        Destination PNG path (parent directories are created).
    frame : str
        Map frame (``"galactic"`` or ``"azel"``) — sets the axis labels.
    metric : str
        Reduction metric — sets the colorbar label and colormap (diverging for
        ``"peak_vlsr"``).
    hpbw_deg : float
        Beam half-power width in degrees (drawn as the resolution circle).
    title : str, optional
        Figure title.

    Returns
    -------
    Path
        The written file's path.
    """
    x_axis, y_axis = gridded.x_axis, gridded.y_axis
    step_x = float(x_axis[1] - x_axis[0]) if x_axis.size > 1 else max(hpbw_deg, 1.0)
    step_y = float(y_axis[1] - y_axis[0]) if y_axis.size > 1 else max(hpbw_deg, 1.0)
    extent = (
        float(x_axis[0]) - step_x / 2.0,
        float(x_axis[-1]) + step_x / 2.0,
        float(y_axis[0]) - step_y / 2.0,
        float(y_axis[-1]) + step_y / 2.0,
    )
    x_label, y_label = _FRAME_LABELS.get(frame, ("x (°)", "y (°)"))
    bar_label, cmap_name = _METRIC_STYLE.get(metric, ("value", "viridis"))

    # NaN cells transparent → the hatched background shows through as "unobserved".
    cmap = plt.get_cmap(cmap_name).with_extremes(bad="none")

    fig, ax = plt.subplots(figsize=(8, 6.5))
    ax.set_facecolor("#e9e9e9")
    ax.patch.set_hatch("xx")
    ax.patch.set_edgecolor("#c2c2c2")
    masked = np.ma.masked_invalid(gridded.grid)
    image = ax.imshow(
        masked,
        origin="lower",
        extent=extent,
        aspect="auto",
        cmap=cmap,
        interpolation="nearest",
    )
    fig.colorbar(image, ax=ax, label=bar_label)

    # The beam: an HPBW-diameter circle, lower-left, so the resolution is visible
    # against the map itself.
    beam_x = extent[0] + max(hpbw_deg, step_x)
    beam_y = extent[2] + max(hpbw_deg, step_y)
    ax.add_patch(
        Circle(
            (beam_x, beam_y),
            hpbw_deg / 2.0,
            fill=False,
            edgecolor="white",
            lw=1.5,
            ls="--",
        )
    )
    ax.annotate(
        "beam",
        (beam_x, beam_y),
        color="white",
        fontsize=7,
        ha="center",
        va="center",
    )
    ax.set_xlabel(x_label)
    ax.set_ylabel(y_label)
    ax.set_title(title or f"Sky map ({metric}, {frame})")
    fig.text(
        0.5,
        0.01,
        f"resolution ≈ {hpbw_deg:.0f}° (HPBW) — each pixel is a beam-smoothed "
        f"average; {gridded.n_samples} pointings, hatched = unobserved",
        ha="center",
        fontsize=7.5,
        color="#555555",
    )
    fig.tight_layout(rect=(0, 0.03, 1, 1))

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
