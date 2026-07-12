"""Offline tests for astro.lsr (plan §4.6).

Fixed scenario: Cygnus A (RA 299.8682°, Dec +40.7339°) from a
Philadelphia-area station (lat 40.02°, lon −75.16°, 100 m) at
2026-01-15T00:00:00 UTC. At that epoch the prototyped astropy truth puts
the topocentric correction at ≈ +11.6 km/s.
"""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pytest

from jansky_observe.astro.lsr import (
    HI_LINE_FREQ_HZ,
    doppler_window_hz,
    topocentric_correction_kms,
    vlsr_axis,
)
from jansky_observe.astro.pointing import target_coord

LAT, LON, ELEV_M = 40.02, -75.16, 100.0
FIXED_UTC = datetime(2026, 1, 15, 0, 0, 0, tzinfo=UTC)
CYG_A_RA, CYG_A_DEC = 299.8682, 40.7339
C_M_PER_S = 299_792_458.0


@pytest.fixture(scope="module")
def cyg_a():
    return target_coord("radec", ra_deg=CYG_A_RA, dec_deg=CYG_A_DEC)


class TestVlsrAxis:
    def test_monotonic_decreasing_in_frequency(self, cyg_a) -> None:
        freq = np.linspace(1419.4e6, 1421.4e6, 401)
        v = vlsr_axis(freq, cyg_a, LAT, LON, ELEV_M, FIXED_UTC)
        assert v.shape == freq.shape
        # Radio convention: higher observed frequency = more negative v_LSR.
        assert np.all(np.diff(v) < 0.0)

    def test_hi_rest_frequency_maps_to_small_velocity(self, cyg_a) -> None:
        v = vlsr_axis(np.array([HI_LINE_FREQ_HZ]), cyg_a, LAT, LON, ELEV_M, FIXED_UTC)
        assert abs(float(v[0])) <= 50.0

    def test_slope_matches_radio_doppler(self, cyg_a) -> None:
        # dv/df = -c/f0 ≈ -0.211 km/s per kHz at the HI line.
        freq = np.array([HI_LINE_FREQ_HZ, HI_LINE_FREQ_HZ + 1e3])
        v = vlsr_axis(freq, cyg_a, LAT, LON, ELEV_M, FIXED_UTC)
        expected = -C_M_PER_S / HI_LINE_FREQ_HZ  # km/s per kHz (units cancel)
        assert (v[1] - v[0]) == pytest.approx(expected, rel=1e-3)


class TestDopplerWindow:
    def test_width_matches_250_kms(self, cyg_a) -> None:
        lo, hi = doppler_window_hz(cyg_a, LAT, LON, ELEV_M, FIXED_UTC, vmax_kms=250.0)
        assert lo < hi
        expected_width = 2.0 * 250.0e3 / C_M_PER_S * HI_LINE_FREQ_HZ  # ≈ 2.37 MHz
        assert (hi - lo) == pytest.approx(expected_width, rel=0.10)

    def test_window_contains_shifted_rest_frequency(self, cyg_a) -> None:
        v_topo = topocentric_correction_kms(cyg_a, LAT, LON, ELEV_M, FIXED_UTC)
        # The topocentric frequency where v_LSR = 0: the rest frequency
        # shifted by the correction (v decreases with f, v(f0) = v_topo).
        f_shifted = HI_LINE_FREQ_HZ * (1.0 + v_topo * 1e3 / C_M_PER_S)
        v_check = vlsr_axis(np.array([f_shifted]), cyg_a, LAT, LON, ELEV_M, FIXED_UTC)
        assert abs(float(v_check[0])) < 0.5
        lo, hi = doppler_window_hz(cyg_a, LAT, LON, ELEV_M, FIXED_UTC)
        assert lo < f_shifted < hi

    def test_window_scales_with_vmax(self, cyg_a) -> None:
        lo1, hi1 = doppler_window_hz(cyg_a, LAT, LON, ELEV_M, FIXED_UTC, vmax_kms=100.0)
        lo2, hi2 = doppler_window_hz(cyg_a, LAT, LON, ELEV_M, FIXED_UTC, vmax_kms=250.0)
        assert lo2 < lo1 < hi1 < hi2


class TestTopocentricCorrection:
    def test_magnitude_bounded(self, cyg_a) -> None:
        # Earth orbit ~30 + rotation ~0.5 + solar LSR ~20 km/s.
        v = topocentric_correction_kms(cyg_a, LAT, LON, ELEV_M, FIXED_UTC)
        assert abs(v) <= 50.0

    def test_fixed_epoch_regression(self, cyg_a) -> None:
        # Prototyped astropy truth for this pointing/time: ≈ +11.6 km/s.
        v = topocentric_correction_kms(cyg_a, LAT, LON, ELEV_M, FIXED_UTC)
        assert v == pytest.approx(11.6, abs=1.0)

    def test_bounded_across_seasons(self, cyg_a) -> None:
        for month in (1, 4, 7, 10):
            when = datetime(2026, month, 15, 0, 0, 0, tzinfo=UTC)
            assert abs(topocentric_correction_kms(cyg_a, LAT, LON, ELEV_M, when)) <= 50.0
