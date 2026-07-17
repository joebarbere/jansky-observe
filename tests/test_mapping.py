"""Sky-map reduction tests (roadmap M11): cell_value, grid_map, the heatmap."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from jansky_observe.astro.lsr import HI_LINE_FREQ_HZ
from jansky_observe.confirm.mapping import GriddedMap, cell_value, grid_map
from jansky_observe.export.figures import sky_map_figure

_WINDOW = (HI_LINE_FREQ_HZ - 1.2e6, HI_LINE_FREQ_HZ + 1.2e6)


def _spectrum(
    *, bump_db: float = 0.0, bump_offset_hz: float = 0.0, base_db: float = -40.0, n: int = 512
) -> tuple[np.ndarray, np.ndarray]:
    """A flat-baseline spectrum with an optional Gaussian line in the window."""
    freq = HI_LINE_FREQ_HZ + np.linspace(-1.5e6, 1.5e6, n)
    power_db = np.full(n, base_db, dtype=np.float64)
    if bump_db:
        center = HI_LINE_FREQ_HZ + bump_offset_hz
        power_db = power_db + bump_db * np.exp(-0.5 * ((freq - center) / 1.5e5) ** 2)
    return freq, power_db


def test_cell_value_hi_intensity_positive_for_a_line() -> None:
    freq, with_line = _spectrum(bump_db=10.0)
    freq, flat = _spectrum(bump_db=0.0)
    line_intensity = cell_value(freq, with_line, metric="hi_intensity", window_hz=_WINDOW)
    flat_intensity = cell_value(freq, flat, metric="hi_intensity", window_hz=_WINDOW)
    assert line_intensity > 0.0
    assert line_intensity > flat_intensity
    assert abs(flat_intensity) < abs(line_intensity)  # noise-only integrates near zero


def test_cell_value_total_power_tracks_the_band_mean() -> None:
    freq, low = _spectrum(base_db=-40.0)
    freq, high = _spectrum(base_db=-30.0)  # 10 dB hotter across the band
    low_tp = cell_value(freq, low, metric="total_power", window_hz=_WINDOW)
    high_tp = cell_value(freq, high, metric="total_power", window_hz=_WINDOW)
    assert high_tp == pytest.approx(low_tp + 10.0, abs=1e-6)


def test_cell_value_peak_vlsr_uses_the_supplied_axis() -> None:
    freq, spec = _spectrum(bump_db=10.0, bump_offset_hz=-3e5)
    vlsr = np.linspace(-200.0, 200.0, freq.size)  # a fake, monotonic LSR axis
    v = cell_value(freq, spec, metric="peak_vlsr", window_hz=_WINDOW, vlsr=vlsr)
    # The peak sits below HI rest (offset −3e5 Hz), i.e. in the lower-freq half →
    # a negative index position on this ascending-freq axis → negative vlsr.
    assert -200.0 <= v < 0.0


def test_cell_value_peak_vlsr_topocentric_without_axis() -> None:
    # A line below the rest frequency is a positive (receding) radio velocity.
    freq, spec = _spectrum(bump_db=10.0, bump_offset_hz=-3e5)
    v = cell_value(freq, spec, metric="peak_vlsr", window_hz=_WINDOW)
    assert v > 0.0


def test_cell_value_unknown_metric_raises() -> None:
    freq, spec = _spectrum()
    with pytest.raises(ValueError, match="unknown metric"):
        cell_value(freq, spec, metric="nonsense", window_hz=_WINDOW)


def test_grid_map_recovers_a_blob_and_leaves_gaps() -> None:
    # Sample a Gaussian blob centred at (l, b) = (30, 0) on a coarse ring of
    # pointings, leaving a corner unobserved.
    xs = np.array([30.0, 20.0, 40.0, 30.0, 30.0], dtype=np.float64)
    ys = np.array([0.0, 0.0, 0.0, 10.0, -10.0], dtype=np.float64)
    vals = np.exp(-0.5 * (((xs - 30.0) / 10.0) ** 2 + ((ys - 0.0) / 10.0) ** 2))
    gm = grid_map(
        xs,
        ys,
        vals,
        center_x_deg=30.0,
        center_y_deg=0.0,
        extent_x_deg=40.0,
        extent_y_deg=40.0,
        step_deg=10.0,
        hpbw_deg=10.0,
    )
    assert isinstance(gm, GriddedMap)
    assert gm.grid.shape == (gm.y_axis.size, gm.x_axis.size)
    assert gm.n_samples == 5
    # The centre cell (30, 0) is the brightest observed cell.
    cx = int(np.argmin(np.abs(gm.x_axis - 30.0)))
    cy = int(np.argmin(np.abs(gm.y_axis - 0.0)))
    assert gm.observed[cy, cx]
    assert gm.grid[cy, cx] == np.nanmax(gm.grid)
    # A far corner with no nearby sample is a gap (NaN), never filled.
    assert np.isnan(gm.grid).any()
    assert not gm.observed.all()


def test_grid_map_empty_is_all_nan() -> None:
    gm = grid_map(
        np.array([]),
        np.array([]),
        np.array([]),
        center_x_deg=0.0,
        center_y_deg=0.0,
        extent_x_deg=20.0,
        extent_y_deg=20.0,
        step_deg=10.0,
        hpbw_deg=10.0,
    )
    assert gm.n_samples == 0
    assert np.isnan(gm.grid).all()
    assert not gm.observed.any()


def test_grid_map_drops_non_finite_samples() -> None:
    xs = np.array([0.0, 10.0, np.nan])
    ys = np.array([0.0, 0.0, 0.0])
    vals = np.array([1.0, np.inf, 2.0])  # one inf value, one nan coord
    gm = grid_map(
        xs,
        ys,
        vals,
        center_x_deg=0.0,
        center_y_deg=0.0,
        extent_x_deg=20.0,
        extent_y_deg=0.0,
        step_deg=10.0,
        hpbw_deg=10.0,
    )
    assert gm.n_samples == 1  # only (0,0)=1.0 survived


@pytest.mark.parametrize("metric", ["hi_intensity", "peak_vlsr", "total_power"])
def test_sky_map_figure_renders_for_each_metric(tmp_path: Path, metric: str) -> None:
    grid = np.array([[1.0, 2.0, np.nan], [3.0, 4.0, 5.0]], dtype=np.float64)
    gm = GriddedMap(
        grid=grid,
        x_axis=np.array([0.0, 10.0, 20.0]),
        y_axis=np.array([0.0, 10.0]),
        observed=np.isfinite(grid),
        n_samples=5,
    )
    out = sky_map_figure(
        gm, tmp_path / f"map-{metric}.png", frame="galactic", metric=metric, hpbw_deg=21.0
    )
    assert out.exists() and out.stat().st_size > 0
