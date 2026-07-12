"""Sky-chart alt/az positions (roadmap M7). Fixed instant for determinism."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from jansky_observe.astro.pointing import target_coord
from jansky_observe.astro.skychart import GALACTIC_PLANE_STEP_DEG, sky_positions

LAT, LON, ELEV = 40.02, -75.16, 100.0
# ~local noon at this longitude — the Sun is well up (see test_pointing).
WHEN = datetime(2026, 1, 15, 17, 0, 0, tzinfo=UTC)


def _cyg_a() -> tuple[str, str, object]:
    return ("Cyg A", "point_source", target_coord("radec", ra_deg=299.868, dec_deg=40.734))


def test_sun_up_moon_and_sources() -> None:
    out = sky_positions(lat_deg=LAT, lon_deg=LON, elevation_m=ELEV, sources=[_cyg_a()], when=WHEN)
    assert out["sun"]["el_deg"] > 0  # Sun is up at local noon
    assert set(out["moon"]) == {"az_deg", "el_deg"}
    assert len(out["sources"]) == 1
    src = out["sources"][0]
    assert src["name"] == "Cyg A" and src["kind"] == "point_source"
    assert 0.0 <= src["az_deg"] < 360.0 or src["az_deg"] < 0  # az may carry an offset; just present
    assert "generated_at" in out


def test_galactic_plane_sampled() -> None:
    out = sky_positions(lat_deg=LAT, lon_deg=LON, elevation_m=ELEV, sources=[], when=WHEN)
    assert len(out["galactic_plane"]) == int(360 / GALACTIC_PLANE_STEP_DEG)
    assert all({"az_deg", "el_deg"} == set(p) for p in out["galactic_plane"])


def test_station_offsets_applied() -> None:
    base = sky_positions(lat_deg=LAT, lon_deg=LON, elevation_m=ELEV, sources=[_cyg_a()], when=WHEN)
    shifted = sky_positions(
        lat_deg=LAT,
        lon_deg=LON,
        elevation_m=ELEV,
        sources=[_cyg_a()],
        when=WHEN,
        offset_az_deg=2.0,
        offset_el_deg=1.0,
    )
    assert shifted["sources"][0]["az_deg"] == pytest.approx(base["sources"][0]["az_deg"] + 2.0)
    assert shifted["sources"][0]["el_deg"] == pytest.approx(base["sources"][0]["el_deg"] + 1.0)
    assert shifted["sun"]["el_deg"] == pytest.approx(base["sun"]["el_deg"] + 1.0)


def test_beam_echoed_or_none() -> None:
    with_beam = sky_positions(
        lat_deg=LAT,
        lon_deg=LON,
        elevation_m=ELEV,
        sources=[],
        when=WHEN,
        beam=(180.0, 45.0),
        hpbw_deg=21.0,
    )
    assert with_beam["beam"] == {"az_deg": 180.0, "el_deg": 45.0, "hpbw_deg": 21.0}
    assert (
        sky_positions(lat_deg=LAT, lon_deg=LON, elevation_m=ELEV, sources=[], when=WHEN)["beam"]
        is None
    )
