"""Offline tests for astro.pointing against precomputed astropy truth.

Truth values were computed once with astropy (bundled IERS, auto-download
off) for Cygnus A (RA 299.8682°, Dec +40.7339°) from a Philadelphia-area
station (lat 40.02°, lon −75.16°, 100 m) at 2026-01-15T00:00:00 UTC:

- az = 307.744°, el = 19.110°
- drift ≈ +0.1205 °/min in az, −0.1518 °/min in el
- transit at 2026-01-15T17:20:18 UTC, el = 89.216°
- sets ≈ 02:29 UTC, rises ≈ 08:15 UTC
- Sun el = +28.9° at 17:00 UTC (local noon), −71.0° at 05:00 UTC (local midnight)
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import astropy.units as u
import pytest
from astropy.coordinates import EarthLocation

from jansky_observe.astro import (
    beam_crossing_minutes,
    hpbw_deg,
    pointing_now,
    rise_set_info,
    target_coord,
    transit_info,
)

LAT, LON, ELEV_M = 40.02, -75.16, 100.0
FIXED_UTC = datetime(2026, 1, 15, 0, 0, 0, tzinfo=UTC)

CYG_A_RA, CYG_A_DEC = 299.8682, 40.7339
TRUTH_AZ, TRUTH_EL = 307.7442, 19.1103
TRUTH_DRIFT_AZ, TRUTH_DRIFT_EL = 0.1205, -0.1518
TRUTH_TRANSIT_UTC = datetime(2026, 1, 15, 17, 20, 18, tzinfo=UTC)
TRUTH_TRANSIT_EL = 89.216
TRUTH_SET_UTC = datetime(2026, 1, 15, 2, 29, 0, tzinfo=UTC)
TRUTH_RISE_UTC = datetime(2026, 1, 15, 8, 15, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def cyg_a():
    return target_coord("radec", ra_deg=CYG_A_RA, dec_deg=CYG_A_DEC)


@pytest.fixture(scope="module")
def location():
    return EarthLocation(lat=LAT * u.deg, lon=LON * u.deg, height=ELEV_M * u.m)


class TestPointingNow:
    def test_cyg_a_az_el(self, cyg_a) -> None:
        info = pointing_now(cyg_a, LAT, LON, ELEV_M, when=FIXED_UTC)
        assert info.az_deg == pytest.approx(TRUTH_AZ, abs=0.5)
        assert info.el_deg == pytest.approx(TRUTH_EL, abs=0.5)
        assert info.is_up

    def test_offsets_shift_display_exactly(self, cyg_a) -> None:
        plain = pointing_now(cyg_a, LAT, LON, ELEV_M, when=FIXED_UTC)
        shifted = pointing_now(
            cyg_a, LAT, LON, ELEV_M, when=FIXED_UTC, offset_az_deg=2.5, offset_el_deg=-1.25
        )
        assert shifted.az_deg == pytest.approx(plain.az_deg + 2.5, abs=1e-9)
        assert shifted.el_deg == pytest.approx(plain.el_deg - 1.25, abs=1e-9)
        # Raw astrometric values are unaffected by the station pointing model.
        assert shifted.raw_az_deg == pytest.approx(plain.raw_az_deg, abs=1e-9)
        assert shifted.raw_el_deg == pytest.approx(plain.raw_el_deg, abs=1e-9)

    def test_drift_rates_finite_and_small(self, cyg_a) -> None:
        info = pointing_now(cyg_a, LAT, LON, ELEV_M, when=FIXED_UTC)
        assert info.drift_az_deg_per_min == pytest.approx(TRUTH_DRIFT_AZ, abs=0.02)
        assert info.drift_el_deg_per_min == pytest.approx(TRUTH_DRIFT_EL, abs=0.02)
        # Sky rotates at 0.25 °/min; nothing should drift faster than ~1 °/min here.
        assert abs(info.drift_az_deg_per_min) < 1.0
        assert abs(info.drift_el_deg_per_min) < 1.0

    def test_sun_is_up_flips_noon_to_midnight(self) -> None:
        noon_utc = datetime(2026, 1, 15, 17, 0, 0, tzinfo=UTC)  # local noon in Philly
        midnight_utc = datetime(2026, 1, 15, 5, 0, 0, tzinfo=UTC)  # local midnight
        sun_noon = target_coord("sun", when=noon_utc)
        sun_midnight = target_coord("sun", when=midnight_utc)
        assert pointing_now(sun_noon, LAT, LON, ELEV_M, when=noon_utc).is_up
        assert not pointing_now(sun_midnight, LAT, LON, ELEV_M, when=midnight_utc).is_up


class TestTargetCoord:
    def test_galactic_matches_icrs(self, cyg_a) -> None:
        gal = cyg_a.galactic
        rebuilt = target_coord("galactic", gal_l_deg=gal.l.deg, gal_b_deg=gal.b.deg)
        assert rebuilt.separation(cyg_a).deg == pytest.approx(0.0, abs=1e-6)

    def test_missing_args_raise(self) -> None:
        with pytest.raises(ValueError, match="radec"):
            target_coord("radec", ra_deg=10.0)
        with pytest.raises(ValueError, match="galactic"):
            target_coord("galactic", gal_l_deg=10.0)
        with pytest.raises(ValueError, match="unknown"):
            target_coord("moon")


class TestTransit:
    def test_cyg_a_transit_elevation_near_89(self, cyg_a, location) -> None:
        # Transit el ≈ 90 − |lat − dec| = 90 − |40.02 − 40.73| ≈ 89.3°.
        info = transit_info(cyg_a, location, when=FIXED_UTC)
        assert info.el_deg == pytest.approx(TRUTH_TRANSIT_EL, abs=1.0)
        assert abs(info.time_utc - TRUTH_TRANSIT_UTC) < timedelta(minutes=5)
        assert info.time_utc.tzinfo is not None


class TestRiseSet:
    def test_cyg_a_rise_and_set(self, cyg_a, location) -> None:
        info = rise_set_info(cyg_a, location, when=FIXED_UTC)
        assert not info.always_up and not info.never_up
        assert info.set_utc is not None and info.rise_utc is not None
        assert abs(info.set_utc - TRUTH_SET_UTC) < timedelta(minutes=5)
        assert abs(info.rise_utc - TRUTH_RISE_UTC) < timedelta(minutes=5)

    def test_circumpolar_target(self, location) -> None:
        polaris_ish = target_coord("radec", ra_deg=37.95, dec_deg=89.26)
        info = rise_set_info(polaris_ish, location, when=FIXED_UTC)
        assert info.always_up
        assert info.rise_utc is None and info.set_utc is None

    def test_never_up_target(self, location) -> None:
        south_pole_star = target_coord("radec", ra_deg=0.0, dec_deg=-89.0)
        info = rise_set_info(south_pole_star, location, when=FIXED_UTC)
        assert info.never_up
        assert info.rise_utc is None and info.set_utc is None


class TestBeamMath:
    def test_hpbw_discovery_dish(self) -> None:
        assert hpbw_deg(0.7) == pytest.approx(21.0, abs=1.0)

    def test_beam_crossing_cyg_a(self) -> None:
        # Plan §4.6: ~1.8 h for Cyg A (δ ≈ 40.7°).
        assert beam_crossing_minutes(40.7) == pytest.approx(110.0, abs=3.0)

    def test_beam_crossing_cas_a(self) -> None:
        # Plan §4.6: ~2.7 h for Cas A (δ ≈ 58.8°).
        assert beam_crossing_minutes(58.8) == pytest.approx(163.0, abs=3.0)

    def test_beam_crossing_near_pole_is_none(self) -> None:
        assert beam_crossing_minutes(89.5) is None
        assert beam_crossing_minutes(-89.5) is None
