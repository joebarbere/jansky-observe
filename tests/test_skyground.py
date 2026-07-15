"""Tests for confirm.skyground (Y-factor sky/ground ΔdB + Tsys, roadmap M10).

The numbers are hand-computed from a clean cold/hot pair: cold sky flat at 0 dB
(linear 1.0), hot ground flat at 10·log10(2) dB (linear 2.0) → Y = 2 exactly, so
ΔdB = 10·log10(2) ≈ 3.0103 dB and, with the default 300 K / 10 K assumptions,
Tsys = (300 − 2·10)/(2 − 1) = 280 K.
"""

from __future__ import annotations

import numpy as np
import pytest

from jansky_observe.confirm.skyground import sky_ground_delta

N = 512


class TestSkyGroundDelta:
    def test_known_ratio_gives_expected_delta_y_tsys(self) -> None:
        cold_db = np.zeros(N)  # linear 1.0
        hot_db = np.full(N, 10.0 * np.log10(2.0))  # linear 2.0 → Y = 2 exactly
        result = sky_ground_delta(cold_db, hot_db)
        assert result["y"] == pytest.approx(2.0)
        assert result["delta_db"] == pytest.approx(10.0 * np.log10(2.0))
        assert result["delta_db"] == pytest.approx(3.0103, abs=1e-3)
        assert result["tsys_k"] == pytest.approx(280.0)  # (300 − 2·10)/(2 − 1)

    def test_custom_temperatures(self) -> None:
        cold_db = np.zeros(N)
        hot_db = np.full(N, 10.0 * np.log10(3.0))  # Y = 3
        result = sky_ground_delta(cold_db, hot_db, t_hot_k=290.0, t_cold_k=5.0)
        assert result["y"] == pytest.approx(3.0)
        # tsys = (290 − 3·5)/(3 − 1) = 275/2 = 137.5
        assert result["tsys_k"] == pytest.approx(137.5)

    def test_band_mean_uses_linear_power(self) -> None:
        # A hot spectrum with structure but the same band-mean linear power as a
        # flat 2.0 must give the same Y (mean is over linear, not dB).
        cold_db = np.zeros(N)
        hot_lin = np.full(N, 2.0)
        hot_lin[: N // 2] = 3.0
        hot_lin[N // 2 :] = 1.0  # mean linear = 2.0
        hot_db = 10.0 * np.log10(hot_lin)
        result = sky_ground_delta(cold_db, hot_db)
        assert result["y"] == pytest.approx(2.0)
        assert result["tsys_k"] == pytest.approx(280.0)

    def test_y_at_or_below_one_is_unphysical(self) -> None:
        cold_db = np.full(N, 10.0 * np.log10(2.0))  # cold hotter than hot
        hot_db = np.zeros(N)
        with pytest.raises(ValueError, match="unphysical Y-factor"):
            sky_ground_delta(cold_db, hot_db)

    def test_equal_power_is_unphysical(self) -> None:
        flat = np.zeros(N)
        with pytest.raises(ValueError, match="unphysical Y-factor"):
            sky_ground_delta(flat, flat)  # Y = 1 exactly

    def test_shape_mismatch_raises(self) -> None:
        with pytest.raises(ValueError, match="shapes must match"):
            sky_ground_delta(np.zeros(N), np.zeros(N - 1))

    def test_tsys_increases_as_y_falls_toward_one(self) -> None:
        # Monotonicity: a smaller Y (colder ground contrast) → a hotter Tsys.
        cold_db = np.zeros(N)
        tsys_big_y = sky_ground_delta(cold_db, np.full(N, 10.0 * np.log10(4.0)))["tsys_k"]
        tsys_small_y = sky_ground_delta(cold_db, np.full(N, 10.0 * np.log10(1.5)))["tsys_k"]
        assert tsys_small_y > tsys_big_y
