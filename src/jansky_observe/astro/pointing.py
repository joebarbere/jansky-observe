"""Pointing math for the session wizard (plan §4.6, §5.1, §5.4).

All functions are pure astropy and fully offline: IERS auto-download is
disabled at import time, so nothing here ever touches the network. Accuracy
with the bundled IERS tables is far better than the ~1° a hand-pointed dish
can use.

Station Δaz/Δel offsets (from the Sun pointing calibration, plan §5.4) are
applied by :func:`pointing_now` so every pointing display shows the numbers
to dial onto the mast scale and angle gauge; the raw astrometric values are
kept alongside.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime

import astropy.units as u
import numpy as np
from astropy.coordinates import AltAz, EarthLocation, SkyCoord, get_sun
from astropy.time import Time
from astropy.utils import iers

# Offline forever: never fetch IERS data, and never complain that the bundled
# tables are stale (arc-second-level errors are irrelevant to a 21° beam).
iers.conf.auto_download = False
iers.conf.auto_max_age = None

__all__ = [
    "PointingInfo",
    "RiseSetInfo",
    "TransitInfo",
    "beam_crossing_minutes",
    "hpbw_deg",
    "local_sidereal_time_hours",
    "pointing_now",
    "rise_set_info",
    "target_coord",
    "transit_info",
]

_SIDEREAL_DEG_PER_HOUR = 15.0
_DRIFT_HALF_WINDOW_S = 30.0
_COARSE_STEP_MIN = 2.0
_SEARCH_HOURS = 24.0


def _to_time(when: datetime | None) -> Time:
    """Convert an optional UTC datetime to an astropy Time (default: now)."""
    if when is None:
        return Time.now()
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return Time(when.astimezone(UTC).replace(tzinfo=None), scale="utc")


def _to_datetime(t: Time) -> datetime:
    """Convert an astropy Time to a timezone-aware UTC datetime."""
    dt: datetime = t.utc.to_datetime()
    return dt.replace(tzinfo=UTC)


def local_sidereal_time_hours(lon_deg: float, when: datetime | None = None) -> float:
    """Apparent local sidereal time at a longitude, in hours [0, 24).

    LST is the right ascension currently on the meridian — the clock an
    observer schedules transits by (the status bar, roadmap M6). Uses astropy's
    apparent sidereal time (nutation included).

    Parameters
    ----------
    lon_deg : float
        East longitude in degrees.
    when : datetime, optional
        UTC instant; the current time by default.

    Returns
    -------
    float
        Apparent LST in hours, in ``[0, 24)``.
    """
    lst = _to_time(when).sidereal_time("apparent", longitude=lon_deg * u.deg)
    return float(lst.hour)


def target_coord(
    kind: str,
    ra_deg: float | None = None,
    dec_deg: float | None = None,
    gal_l_deg: float | None = None,
    gal_b_deg: float | None = None,
    when: datetime | None = None,
) -> SkyCoord:
    """Build the SkyCoord for an observing target.

    Parameters
    ----------
    kind : str
        One of ``"radec"`` (ICRS degrees), ``"galactic"`` (l/b degrees), or
        ``"sun"`` (astropy ``get_sun`` at ``when``).
    ra_deg, dec_deg : float, optional
        ICRS coordinates in degrees; required when ``kind`` is ``"radec"``.
    gal_l_deg, gal_b_deg : float, optional
        Galactic coordinates in degrees; required when ``kind`` is
        ``"galactic"``.
    when : datetime, optional
        UTC time for the Sun ephemeris (default: now). Ignored for fixed
        targets.

    Returns
    -------
    SkyCoord
        The target coordinate (ICRS frame for fixed targets, GCRS for the
        Sun).

    Raises
    ------
    ValueError
        If ``kind`` is unknown or the required coordinates are missing.
    """
    if kind == "sun":
        return get_sun(_to_time(when))
    if kind == "radec":
        if ra_deg is None or dec_deg is None:
            raise ValueError("kind='radec' requires ra_deg and dec_deg")
        return SkyCoord(ra=ra_deg * u.deg, dec=dec_deg * u.deg, frame="icrs")
    if kind == "galactic":
        if gal_l_deg is None or gal_b_deg is None:
            raise ValueError("kind='galactic' requires gal_l_deg and gal_b_deg")
        return SkyCoord(l=gal_l_deg * u.deg, b=gal_b_deg * u.deg, frame="galactic").icrs
    raise ValueError(f"unknown target kind: {kind!r} (expected 'radec', 'galactic', or 'sun')")


@dataclass(frozen=True)
class PointingInfo:
    """Where to point the dish right now.

    ``az_deg``/``el_deg`` include the station Δaz/Δel offsets (plan §5.4) —
    these are the numbers to dial onto the mast scale and angle gauge.
    ``raw_az_deg``/``raw_el_deg`` are the astrometric values without offsets.
    """

    az_deg: float
    el_deg: float
    raw_az_deg: float
    raw_el_deg: float
    drift_az_deg_per_min: float
    drift_el_deg_per_min: float
    is_up: bool


@dataclass(frozen=True)
class TransitInfo:
    """Next transit (culmination) of a target."""

    time_utc: datetime
    el_deg: float


@dataclass(frozen=True)
class RiseSetInfo:
    """Next rise and set times of a target.

    Either time is ``None`` when the horizon crossing does not occur in the
    next 24 h; ``always_up``/``never_up`` distinguish circumpolar targets
    from ones that never clear the horizon.
    """

    rise_utc: datetime | None
    set_utc: datetime | None
    always_up: bool
    never_up: bool


def _altaz(coord: SkyCoord, location: EarthLocation, t: Time) -> SkyCoord:
    """Transform ``coord`` to the AltAz frame at ``location`` and time ``t``."""
    return coord.transform_to(AltAz(obstime=t, location=location))


def pointing_now(
    coord: SkyCoord,
    lat_deg: float,
    lon_deg: float,
    elevation_m: float,
    when: datetime | None = None,
    offset_az_deg: float = 0.0,
    offset_el_deg: float = 0.0,
) -> PointingInfo:
    """Current az/el of a target with station offsets applied.

    Parameters
    ----------
    coord : SkyCoord
        Target from :func:`target_coord`.
    lat_deg, lon_deg, elevation_m : float
        Station location (geodetic degrees, metres).
    when : datetime, optional
        UTC time (default: now).
    offset_az_deg, offset_el_deg : float
        Station Δaz/Δel pointing-model offsets from the Sun calibration
        (plan §5.4); added to the raw values for the displayed az/el.

    Returns
    -------
    PointingInfo
        Displayed and raw az/el, drift rates (finite difference over ±30 s),
        and whether the target is up (raw elevation > 0).
    """
    location = EarthLocation(lat=lat_deg * u.deg, lon=lon_deg * u.deg, height=elevation_m * u.m)
    t = _to_time(when)

    now = _altaz(coord, location, t)
    before = _altaz(coord, location, t - _DRIFT_HALF_WINDOW_S * u.s)
    after = _altaz(coord, location, t + _DRIFT_HALF_WINDOW_S * u.s)

    span_min = 2.0 * _DRIFT_HALF_WINDOW_S / 60.0
    d_az = (after.az.deg - before.az.deg + 180.0) % 360.0 - 180.0
    d_el = after.alt.deg - before.alt.deg

    raw_az = float(now.az.deg)
    raw_el = float(now.alt.deg)
    return PointingInfo(
        az_deg=(raw_az + offset_az_deg) % 360.0,
        el_deg=raw_el + offset_el_deg,
        raw_az_deg=raw_az,
        raw_el_deg=raw_el,
        drift_az_deg_per_min=float(d_az) / span_min,
        drift_el_deg_per_min=float(d_el) / span_min,
        is_up=raw_el > 0.0,
    )


def _sample_elevations(
    coord: SkyCoord, location: EarthLocation, start: Time
) -> tuple[Time, np.ndarray]:
    """Sample target elevation over the next 24 h at ~2 min resolution."""
    offsets = np.arange(0.0, _SEARCH_HOURS * 60.0 + _COARSE_STEP_MIN, _COARSE_STEP_MIN)
    times = start + offsets * u.min
    els = _altaz(coord, location, times).alt.deg
    return times, np.asarray(els)


def transit_info(
    coord: SkyCoord, location: EarthLocation, when: datetime | None = None
) -> TransitInfo:
    """Next transit time (UTC) and transit elevation.

    Samples the next 24 h at ~2 min resolution, then refines the maximum at
    1 s resolution over a ±2 min window.

    Parameters
    ----------
    coord : SkyCoord
        Target coordinate.
    location : EarthLocation
        Station location.
    when : datetime, optional
        UTC search start (default: now).

    Returns
    -------
    TransitInfo
        Time and elevation of the highest point in the next 24 h.
    """
    start = _to_time(when)
    times, els = _sample_elevations(coord, location, start)
    i = int(np.argmax(els))

    fine = times[i] + np.arange(-_COARSE_STEP_MIN * 60.0, _COARSE_STEP_MIN * 60.0 + 1.0, 1.0) * u.s
    fine_els = np.asarray(_altaz(coord, location, fine).alt.deg)
    j = int(np.argmax(fine_els))
    return TransitInfo(time_utc=_to_datetime(fine[j]), el_deg=float(fine_els[j]))


def _refine_crossing(coord: SkyCoord, location: EarthLocation, lo: Time, hi: Time) -> datetime:
    """Bisect a horizon crossing bracketed by ``lo``/``hi`` to ~1 s."""
    el_lo = float(_altaz(coord, location, lo).alt.deg)
    while (hi - lo).sec > 1.0:
        mid = lo + (hi - lo) / 2
        el_mid = float(_altaz(coord, location, mid).alt.deg)
        if (el_lo > 0.0) == (el_mid > 0.0):
            lo, el_lo = mid, el_mid
        else:
            hi = mid
    return _to_datetime(lo + (hi - lo) / 2)


def rise_set_info(
    coord: SkyCoord, location: EarthLocation, when: datetime | None = None
) -> RiseSetInfo:
    """Next rise and set times over the coming 24 h.

    Parameters
    ----------
    coord : SkyCoord
        Target coordinate.
    location : EarthLocation
        Station location.
    when : datetime, optional
        UTC search start (default: now).

    Returns
    -------
    RiseSetInfo
        Next rise/set (UTC, refined to ~1 s) or ``None`` for each when no
        crossing occurs; ``always_up``/``never_up`` flag circumpolar and
        never-visible targets.
    """
    start = _to_time(when)
    times, els = _sample_elevations(coord, location, start)
    up = els > 0.0

    rise: datetime | None = None
    set_: datetime | None = None
    for k in np.where(up[:-1] != up[1:])[0]:
        crossing = _refine_crossing(coord, location, times[int(k)], times[int(k) + 1])
        if up[k + 1] and rise is None:
            rise = crossing
        elif not up[k + 1] and set_ is None:
            set_ = crossing
        if rise is not None and set_ is not None:
            break

    no_crossings = rise is None and set_ is None
    return RiseSetInfo(
        rise_utc=rise,
        set_utc=set_,
        always_up=no_crossings and bool(up.all()),
        never_up=no_crossings and not bool(up.any()),
    )


def hpbw_deg(dish_diameter_m: float, freq_hz: float = 1420.4e6) -> float:
    """Half-power beam width in degrees: 70·λ/D (plan §4.6).

    Parameters
    ----------
    dish_diameter_m : float
        Dish diameter in metres (0.7 for the Discovery Dish).
    freq_hz : float
        Observing frequency in Hz (default: the HI line).

    Returns
    -------
    float
        HPBW in degrees (≈21° for a 0.7 m dish at 1420 MHz).
    """
    wavelength_m = 299_792_458.0 / freq_hz
    return 70.0 * wavelength_m / dish_diameter_m


def beam_crossing_minutes(dec_deg: float, hpbw_deg: float = 21.0) -> float | None:
    """How long a drifting source stays in the beam, in minutes (plan §4.6).

    Crossing time ≈ HPBW / (15°·cos δ per hour) — e.g. ~1.8 h for Cyg A,
    ~2.7 h for Cas A with the 21° beam.

    Parameters
    ----------
    dec_deg : float
        Source declination in degrees.
    hpbw_deg : float
        Beam width in degrees (default 21).

    Returns
    -------
    float or None
        Crossing time in minutes, or ``None`` near the pole (|δ| ≥ 89°,
        where the source effectively never leaves the beam).
    """
    if abs(dec_deg) >= 89.0:
        return None
    rate_deg_per_hour = _SIDEREAL_DEG_PER_HOUR * math.cos(math.radians(dec_deg))
    return hpbw_deg / rate_deg_per_hour * 60.0
