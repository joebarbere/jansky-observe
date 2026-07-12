"""Alt/az sky positions for the in-UI sky chart (roadmap M7).

The always-available, fully-offline glance at what's up: the seeded catalog
sources, the Sun and Moon, the galactic plane, and (when a session is running)
the dish's beam cone — all from astropy, no network, no GL. Desktop Stellarium
keeps its M5 cross-check role; this is the version that's always there.

:func:`sky_positions` is pure astropy (it takes already-built ``SkyCoord``s), so
it unit-tests without the database or the server. Azimuth is degrees east of
north; elevation is degrees above the horizon (negative = below).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import astropy.units as u
import numpy as np
from astropy.coordinates import AltAz, EarthLocation, SkyCoord, get_body, get_sun

from jansky_observe.astro.pointing import _to_datetime, _to_time

__all__ = ["GALACTIC_PLANE_STEP_DEG", "sky_positions"]

GALACTIC_PLANE_STEP_DEG = 6.0
"""Sampling step in galactic longitude for the plane curve (0…360°)."""


def sky_positions(
    *,
    lat_deg: float,
    lon_deg: float,
    elevation_m: float,
    sources: list[tuple[str, str, SkyCoord]],
    when: datetime | None = None,
    offset_az_deg: float = 0.0,
    offset_el_deg: float = 0.0,
    beam: tuple[float, float] | None = None,
    hpbw_deg: float = 21.0,
) -> dict[str, Any]:
    """Compute az/el for the sky-chart layers at ``when`` (default: now).

    Parameters
    ----------
    lat_deg, lon_deg, elevation_m : float
        Observing location (geodetic degrees, metres).
    sources : list of (name, kind, SkyCoord)
        Catalog targets to place; ``kind`` is echoed for styling. Sun-kind
        sources should be left out — the Sun is drawn as its own symbol.
    when : datetime, optional
        UTC instant.
    offset_az_deg, offset_el_deg : float
        Station Δaz/Δel offsets (plan §5.4), added to every computed az/el so the
        chart matches what the operator dials.
    beam : (az_deg, el_deg), optional
        The dish's current dialed pointing (a running session); echoed with
        ``hpbw_deg`` as the beam cone. ``None`` = no cone.
    hpbw_deg : float
        Beam half-power beamwidth for the cone (21° at 1420 MHz on 700 mm).

    Returns
    -------
    dict
        ``{"generated_at", "sources", "sun", "moon", "galactic_plane", "beam"}``.
    """
    t = _to_time(when)
    location = EarthLocation(lat=lat_deg * u.deg, lon=lon_deg * u.deg, height=elevation_m * u.m)
    frame = AltAz(obstime=t, location=location)

    def azel(coord: SkyCoord) -> dict[str, float]:
        altaz = coord.transform_to(frame)
        return {
            "az_deg": float(altaz.az.deg) + offset_az_deg,
            "el_deg": float(altaz.alt.deg) + offset_el_deg,
        }

    source_rows = [{"name": name, "kind": kind, **azel(coord)} for name, kind, coord in sources]

    lon = np.arange(0.0, 360.0, GALACTIC_PLANE_STEP_DEG)
    plane = SkyCoord(l=lon * u.deg, b=np.zeros_like(lon) * u.deg, frame="galactic")
    plane_altaz = plane.transform_to(frame)
    galactic_plane = [
        {"az_deg": float(a) + offset_az_deg, "el_deg": float(e) + offset_el_deg}
        for a, e in zip(plane_altaz.az.deg, plane_altaz.alt.deg, strict=True)
    ]

    beam_row = (
        {"az_deg": beam[0], "el_deg": beam[1], "hpbw_deg": hpbw_deg} if beam is not None else None
    )
    return {
        "generated_at": _to_datetime(t).isoformat(),
        "sources": source_rows,
        "sun": azel(get_sun(t)),
        "moon": azel(get_body("moon", t, location)),
        "galactic_plane": galactic_plane,
        "beam": beam_row,
    }
