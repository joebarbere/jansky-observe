"""LSR correction: topocentric frequency ↔ v_LSR (plan §4.6).

The observed HI frequency is shifted by Earth rotation + orbit + solar
motion relative to the Local Standard of Rest. This module converts a
topocentric frequency axis to v_LSR for a given pointing and time using
astropy ``SpectralCoord``: the observer is the station at rest in ITRS
(an explicit zero ITRS velocity — the frame transform then supplies the
Earth-rotation and orbital velocity), the target is the pointing direction
with a nominal 1 kpc distance (a finite distance keeps ``SpectralCoord``
off its no-distance warning path; the radial velocity is pinned to zero
so only the observer's motion enters). ``with_observer_stationary_relative_to("lsrk")``
shifts to the kinematic LSR, and the radio Doppler convention against the
HI rest frequency yields km/s.

All functions are pure astropy and fully offline: IERS auto-download is
disabled at import time, same as :mod:`jansky_observe.astro.pointing`.
"""

from __future__ import annotations

from datetime import UTC, datetime

import astropy.units as u
import numpy as np
from astropy.coordinates import (
    ITRS,
    CartesianDifferential,
    EarthLocation,
    SkyCoord,
    SpectralCoord,
)
from astropy.time import Time
from astropy.utils import iers

# Offline forever: never fetch IERS data, and never complain that the bundled
# tables are stale (sub-arc-second errors are irrelevant to km/s velocities).
iers.conf.auto_download = False
iers.conf.auto_max_age = None

__all__ = [
    "HI_LINE_FREQ_HZ",
    "doppler_window_hz",
    "topocentric_correction_kms",
    "vlsr_axis",
]

HI_LINE_FREQ_HZ = 1_420_405_751.7667
"""Rest frequency of the neutral-hydrogen 21 cm line (Hz)."""

_C_M_PER_S = 299_792_458.0
_TARGET_DISTANCE_KPC = 1.0
_WINDOW_GRID_POINTS = 8001
_WINDOW_MARGIN_KMS = 100.0


def _to_time(when: datetime) -> Time:
    """Convert a UTC datetime (naive treated as UTC) to an astropy Time."""
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return Time(when.astimezone(UTC).replace(tzinfo=None), scale="utc")


def _observer(lat_deg: float, lon_deg: float, elevation_m: float, when: datetime) -> ITRS:
    """The station as an ITRS coordinate at ``when`` with zero ITRS velocity.

    Zero velocity in ITRS is exactly right: the station co-rotates with the
    Earth, and the ITRS→LSRK frame transform supplies rotation + orbit.
    """
    location = EarthLocation(lat=lat_deg * u.deg, lon=lon_deg * u.deg, height=elevation_m * u.m)
    t = _to_time(when)
    itrs = location.get_itrs(obstime=t)
    zero_velocity = CartesianDifferential([0.0, 0.0, 0.0] * (u.km / u.s))
    return ITRS(itrs.data.with_differentials(zero_velocity), obstime=t)


def _target(coord: SkyCoord) -> SkyCoord:
    """The pointing direction as an ICRS target with a nominal distance.

    ``SpectralCoord`` needs the target to carry a finite distance and a
    radial velocity; the nominal 1 kpc and 0 km/s pin the transform to the
    observer's motion only (direction is all that matters here).
    """
    icrs = coord.icrs
    return SkyCoord(
        ra=icrs.ra,
        dec=icrs.dec,
        distance=_TARGET_DISTANCE_KPC * u.kpc,
        radial_velocity=0.0 * (u.km / u.s),
        frame="icrs",
    )


def vlsr_axis(
    freq_hz: np.ndarray,
    coord: SkyCoord,
    lat_deg: float,
    lon_deg: float,
    elevation_m: float,
    when: datetime,
) -> np.ndarray:
    """Convert topocentric frequencies to v_LSR for a pointing and time.

    Parameters
    ----------
    freq_hz : numpy.ndarray
        Topocentric (observed) frequencies in Hz.
    coord : SkyCoord
        Pointing direction (any frame; converted to ICRS).
    lat_deg, lon_deg, elevation_m : float
        Station location (geodetic degrees, metres).
    when : datetime
        UTC observation time (naive treated as UTC).

    Returns
    -------
    numpy.ndarray
        v_LSR in km/s (radio convention against the HI rest frequency),
        one value per input frequency. Monotonically *decreasing* in
        frequency: higher observed frequency = blueshift = more negative
        velocity.
    """
    spectral = SpectralCoord(
        np.asarray(freq_hz, dtype=np.float64) * u.Hz,
        observer=_observer(lat_deg, lon_deg, elevation_m, when),
        target=_target(coord),
    )
    velocity = spectral.with_observer_stationary_relative_to("lsrk").to(
        u.km / u.s, doppler_rest=HI_LINE_FREQ_HZ * u.Hz, doppler_convention="radio"
    )
    return np.asarray(velocity.value, dtype=np.float64)


def doppler_window_hz(
    coord: SkyCoord,
    lat_deg: float,
    lon_deg: float,
    elevation_m: float,
    when: datetime,
    vmax_kms: float = 250.0,
) -> tuple[float, float]:
    """Topocentric frequency window for |v_LSR| ≤ ``vmax_kms`` (plan §6).

    Inverts :func:`vlsr_axis` by brute force: evaluate v_LSR on a fine
    frequency grid around the HI rest frequency and interpolate the
    frequencies at ±``vmax_kms``.

    Parameters
    ----------
    coord : SkyCoord
        Pointing direction.
    lat_deg, lon_deg, elevation_m : float
        Station location (geodetic degrees, metres).
    when : datetime
        UTC observation time.
    vmax_kms : float
        Half-width of the velocity window in km/s (default 250, galactic
        HI's plausible range).

    Returns
    -------
    tuple of float
        ``(lo_hz, hi_hz)`` topocentric frequency bounds, ``lo_hz < hi_hz``.
    """
    # Grid wide enough to bracket ±vmax plus the ≤50 km/s topocentric shift.
    half_span_hz = (vmax_kms + _WINDOW_MARGIN_KMS) * 1e3 / _C_M_PER_S * HI_LINE_FREQ_HZ
    grid = HI_LINE_FREQ_HZ + np.linspace(-half_span_hz, half_span_hz, _WINDOW_GRID_POINTS)
    velocity = vlsr_axis(grid, coord, lat_deg, lon_deg, elevation_m, when)
    # v_LSR decreases with frequency; sort for np.interp's increasing-x rule.
    order = np.argsort(velocity)
    f_at_plus = float(np.interp(vmax_kms, velocity[order], grid[order]))
    f_at_minus = float(np.interp(-vmax_kms, velocity[order], grid[order]))
    return (min(f_at_plus, f_at_minus), max(f_at_plus, f_at_minus))


def topocentric_correction_kms(
    coord: SkyCoord,
    lat_deg: float,
    lon_deg: float,
    elevation_m: float,
    when: datetime,
) -> float:
    """The v_LSR that the HI rest frequency maps to at this pointing/time.

    A source at rest in the topocentric frame appears at this v_LSR — the
    scalar LSR correction, useful in tests and status displays. Its
    magnitude is bounded by Earth's orbital (~30 km/s) plus rotational
    (~0.5 km/s) plus solar-LSR (~20 km/s) motion, so always ≲ 50 km/s.

    Parameters
    ----------
    coord : SkyCoord
        Pointing direction.
    lat_deg, lon_deg, elevation_m : float
        Station location (geodetic degrees, metres).
    when : datetime
        UTC observation time.

    Returns
    -------
    float
        v_LSR of the HI rest frequency in km/s.
    """
    velocity = vlsr_axis(np.array([HI_LINE_FREQ_HZ]), coord, lat_deg, lon_deg, elevation_m, when)
    return float(velocity[0])
