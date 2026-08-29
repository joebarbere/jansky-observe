"""Observed-vs-model comparison in kelvin (plans/calibrated-overlay.md).

The load-bearing test here is :func:`test_recovers_a_pure_scale_error`. Pierre Terrier's
2025 amateur rotation-curve study overlaid LAB on every spectrum and still could not see
that its temperatures ran at roughly 0.65 of the survey's, because the overlay compared
shapes on twin axes. This is that defect encoded as a regression: a spectrum scaled by a
constant must report that constant.
"""

from __future__ import annotations

import numpy as np
import pytest

from jansky_observe.confirm.overlay import MIN_OVERLAP_CHANNELS, compare_to_model


def _profile(
    n: int = 256, *, peak_k: float = 60.0, center_kms: float = 0.0
) -> tuple[np.ndarray, np.ndarray]:
    v = np.linspace(-200.0, 200.0, n)
    return v, peak_k * np.exp(-0.5 * ((v - center_kms) / 25.0) ** 2)


def test_recovers_a_pure_scale_error() -> None:
    """A 0.65x-scaled observation reports scale_ratio 0.65 — the Terrier regression."""
    v_model, model = _profile()
    v_obs, obs = v_model, model * 0.65

    result = compare_to_model(v_obs, obs, v_model, model)

    assert result is not None
    assert result.scale_ratio == pytest.approx(0.65, rel=1e-6)
    assert result.peak_dv_kms == pytest.approx(0.0, abs=1e-9)
    assert result.n_channels == 256


def test_matching_spectra_report_unity_and_no_residual() -> None:
    v, model = _profile()
    result = compare_to_model(v, model, v, model)

    assert result is not None
    assert result.scale_ratio == pytest.approx(1.0, rel=1e-9)
    assert result.residual_rms_k == pytest.approx(0.0, abs=1e-9)
    assert np.allclose(result.residual_k, 0.0)


def test_velocity_offset_shows_up_as_peak_dv() -> None:
    v_model, model = _profile()
    v_obs, obs = _profile(center_kms=20.0)

    result = compare_to_model(v_obs, obs, v_model, model)

    assert result is not None
    # 400 km/s over 256 channels ≈ 1.57 km/s per channel.
    assert result.peak_dv_kms == pytest.approx(20.0, abs=2.0)


def test_reversed_model_axis_is_handled() -> None:
    """v_LSR decreases with frequency, so a model axis may arrive descending."""
    v_model, model = _profile()
    result = compare_to_model(v_model, model * 0.5, v_model[::-1], model[::-1])

    assert result is not None
    assert result.scale_ratio == pytest.approx(0.5, rel=1e-6)


def test_partial_overlap_reports_the_range_it_used() -> None:
    v_model, model = _profile()
    v_obs, obs = _profile(n=128)
    v_obs = v_obs / 2.0  # observed spans only -100..100

    result = compare_to_model(v_obs, obs, v_model, model)

    assert result is not None
    assert result.overlap_kms[0] == pytest.approx(-100.0)
    assert result.overlap_kms[1] == pytest.approx(100.0)
    assert result.n_channels == 128


def test_too_little_overlap_returns_none() -> None:
    v_model, model = _profile()
    v_obs = np.linspace(199.0, 400.0, MIN_OVERLAP_CHANNELS - 1)
    assert compare_to_model(v_obs, np.ones_like(v_obs), v_model, model) is None


def test_disjoint_axes_return_none() -> None:
    v_model, model = _profile()
    v_obs = np.linspace(500.0, 700.0, 64)
    assert compare_to_model(v_obs, np.ones_like(v_obs), v_model, model) is None


def test_zero_model_returns_none() -> None:
    """No scale ratio is defined against a model that is identically zero."""
    v, _ = _profile()
    assert compare_to_model(v, np.ones_like(v), v, np.zeros_like(v)) is None


def test_empty_input_returns_none() -> None:
    v, model = _profile()
    empty = np.asarray([], dtype=np.float64)
    assert compare_to_model(empty, empty, v, model) is None


def test_stats_is_json_able_and_array_free() -> None:
    v, model = _profile()
    result = compare_to_model(v, model * 0.65, v, model)
    assert result is not None

    stats = result.stats()
    assert set(stats) == {
        "scale_ratio",
        "residual_rms_k",
        "peak_dv_kms",
        "observed_peak_k",
        "model_peak_k",
        "overlap_kms",
        "n_channels",
    }
    assert not any(isinstance(value, np.ndarray) for value in stats.values())


@pytest.mark.parametrize("bad", ["observed", "model"])
def test_mismatched_shapes_raise(bad: str) -> None:
    v, model = _profile()
    args = (v, model, v, model)
    if bad == "observed":
        args = (v[:-1], model, v, model)
        match = "observed axis/values"
    else:
        args = (v, model, v[:-1], model)
        match = "model axis/values"
    with pytest.raises(ValueError, match=match):
        compare_to_model(*args)
