"""HI sky mapping (roadmap M11): build a gridded map, and raster to fill one.

Two responsibilities, mirroring the M7 scheduler / M9 tracking split:

* **Build (read path).** :func:`build_sky_map` reduces every capture tagged into a
  :class:`~jansky_observe.models.SkyMap` to a per-cell scalar
  (:func:`jansky_observe.confirm.mapping.cell_value`), projects each capture's
  commanded az/el into the map's frame (az/el as-is, or → galactic l/b at the
  capture's time), and bins them onto the grid
  (:func:`jansky_observe.confirm.mapping.grid_map`). Pure of hardware; used by the
  routes, the PDF report, and the observation bundle.

* **Acquire (raster runner).** :func:`map_tick` slews the rotator across an
  **az/el** grid, dwelling at each cell to record a capture, and tags it into the
  map. It moves hardware **only** through the guarded
  :func:`jansky_observe.server.rotator.slew` primitive and drives the daemon over
  the same control channel the UI uses — no new device path, no bias-tee contact.
  The re-point decision (:func:`next_map_cell`) is pure and unit-testable.

Galactic-frame maps are filled by **ingesting drift/campaign captures**, not by
the runner (a live raster is naturally an az/el patch); the build path converts
their az/el to l/b, so both frames render.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

import astropy.units as u
import numpy as np
from astropy.coordinates import AltAz, EarthLocation, SkyCoord
from astropy.time import Time
from sqlmodel import Session, col, select

from jansky_observe.astro.lsr import doppler_window_hz, vlsr_axis
from jansky_observe.astro.pointing import hpbw_deg
from jansky_observe.confirm.classifier import FIXED_WINDOW_HALF_WIDTH_HZ, averaged_spectrum
from jansky_observe.confirm.mapping import GriddedMap, cell_value, grid_map
from jansky_observe.control import ctl_request
from jansky_observe.models import Capture, Location, RadioSource, SkyMap, Station
from jansky_observe.server.rotator import (
    RotatorError,
    RotatorUnconfigured,
    SlewOutOfLimits,
    slew,
    station_allows,
)

if TYPE_CHECKING:  # pragma: no cover
    from fastapi import FastAPI

logger = logging.getLogger(__name__)

__all__ = [
    "MAP_TICK_S",
    "MapRunState",
    "azel_to_galactic",
    "build_sky_map",
    "map_cells",
    "map_loop",
    "map_summary",
    "map_tick",
    "next_map_cell",
    "sky_map_samples",
    "start_map",
    "stop_map",
]

MAP_TICK_S = 5.0
"""Seconds between raster evaluations (a cell itself takes ``dwell_s``)."""
CTL_TIMEOUT_MS = 1500
_CELL_MATCH_DEG = 0.5
"""A grid cell counts as already-observed if a capture sits within this az/el."""


# ---- frame projection --------------------------------------------------------------


def azel_to_galactic(
    az_deg: float,
    el_deg: float,
    lat_deg: float,
    lon_deg: float,
    elevation_m: float,
    when: datetime,
) -> tuple[float, float]:
    """Galactic (l, b) degrees for an az/el pointing at a location and time."""
    location = EarthLocation(lat=lat_deg * u.deg, lon=lon_deg * u.deg, height=elevation_m * u.m)
    altaz = SkyCoord(
        az=az_deg * u.deg,
        alt=el_deg * u.deg,
        frame=AltAz(obstime=Time(when), location=location),
    )
    gal = altaz.transform_to("galactic")
    return float(gal.l.deg), float(gal.b.deg)


def _altaz_coord(az_deg: float, el_deg: float, location: Location, when: datetime) -> SkyCoord:
    """The ICRS SkyCoord an az/el pointing looks at (for the LSR velocity axis)."""
    earth = EarthLocation(
        lat=location.lat_deg * u.deg,
        lon=location.lon_deg * u.deg,
        height=location.elevation_m * u.m,
    )
    return SkyCoord(
        az=az_deg * u.deg,
        alt=el_deg * u.deg,
        frame=AltAz(obstime=Time(when), location=earth),
    ).icrs


# ---- build (read path) -------------------------------------------------------------


def _fixed_window() -> tuple[float, float]:
    from jansky_observe.astro.lsr import HI_LINE_FREQ_HZ

    return (
        HI_LINE_FREQ_HZ - FIXED_WINDOW_HALF_WIDTH_HZ,
        HI_LINE_FREQ_HZ + FIXED_WINDOW_HALF_WIDTH_HZ,
    )


def sky_map_samples(
    session: Session, sky_map: SkyMap, location: Location, metric: str | None = None
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Reduce a map's captures to ``(x_deg, y_deg, value)`` sample arrays.

    Each capture's commanded az/el is projected into the map's frame (az/el, or
    galactic l/b at the capture's time), and its averaged spectrum reduced to the
    metric (``metric`` override, else the map's default). Captures whose file is
    gone or whose reduction is degenerate are **dropped** (they become gaps in the
    grid) — never faked.
    """
    metric = metric or sky_map.metric
    window = _fixed_window()
    captures = session.exec(
        select(Capture).where(
            Capture.sky_map_id == sky_map.id,
            col(Capture.purged_at).is_(None),
            Capture.format == "npz_spectra",
            col(Capture.map_az_deg).is_not(None),
            col(Capture.map_el_deg).is_not(None),
        )
    ).all()
    xs: list[float] = []
    ys: list[float] = []
    values: list[float] = []
    for capture in captures:
        az, el = float(capture.map_az_deg), float(capture.map_el_deg)  # type: ignore[arg-type]
        when = capture.start or capture.created_at
        try:
            freq_hz, power_db = averaged_spectrum(capture.path)
            vlsr = None
            if metric == "peak_vlsr":
                coord = _altaz_coord(az, el, location, when)
                vlsr = vlsr_axis(
                    freq_hz, coord, location.lat_deg, location.lon_deg, location.elevation_m, when
                )
                window = doppler_window_hz(
                    coord, location.lat_deg, location.lon_deg, location.elevation_m, when
                )
            value = cell_value(freq_hz, power_db, metric=metric, window_hz=window, vlsr=vlsr)
        except (FileNotFoundError, ValueError):
            continue  # missing file or degenerate spectrum → drop this cell
        if sky_map.frame == "galactic":
            x, y = azel_to_galactic(
                az, el, location.lat_deg, location.lon_deg, location.elevation_m, when
            )
        else:
            x, y = az, el
        xs.append(x)
        ys.append(y)
        values.append(value)
    return (
        np.asarray(xs, dtype=np.float64),
        np.asarray(ys, dtype=np.float64),
        np.asarray(values, dtype=np.float64),
    )


def build_sky_map(
    session: Session,
    sky_map: SkyMap,
    station: Station,
    location: Location,
    metric: str | None = None,
) -> GriddedMap:
    """Reduce + grid a map's captures into a :class:`GriddedMap` (beam-limited)."""
    xs, ys, values = sky_map_samples(session, sky_map, location, metric)
    return grid_map(
        xs,
        ys,
        values,
        center_x_deg=sky_map.center_x_deg,
        center_y_deg=sky_map.center_y_deg,
        extent_x_deg=sky_map.extent_x_deg,
        extent_y_deg=sky_map.extent_y_deg,
        step_deg=sky_map.step_deg,
        hpbw_deg=hpbw_deg(station.dish_diameter_m),
    )


def map_summary(
    session: Session, sky_map: SkyMap, station: Station, location: Location
) -> dict[str, Any]:
    """A JSON-able summary of a map: geometry, coverage, and the gridded values."""
    gridded = build_sky_map(session, sky_map, station, location)
    source = session.get(RadioSource, sky_map.source_id) if sky_map.source_id else None
    cells = map_cells(sky_map)
    grid = gridded.grid
    return {
        "id": sky_map.id,
        "name": sky_map.name,
        "source": None if source is None else source.name,
        "frame": sky_map.frame,
        "metric": sky_map.metric,
        "status": sky_map.status,
        "center_x_deg": sky_map.center_x_deg,
        "center_y_deg": sky_map.center_y_deg,
        "extent_x_deg": sky_map.extent_x_deg,
        "extent_y_deg": sky_map.extent_y_deg,
        "step_deg": sky_map.step_deg,
        "dwell_s": sky_map.dwell_s,
        "hpbw_deg": hpbw_deg(station.dish_diameter_m),
        "n_samples": gridded.n_samples,
        "n_cells_total": len(cells),
        "n_cells_observed": int(gridded.observed.sum()),
        "x_axis": gridded.x_axis.tolist(),
        "y_axis": gridded.y_axis.tolist(),
        "grid": [[None if not np.isfinite(v) else float(v) for v in row] for row in grid],
        "notes": sky_map.notes,
    }


# ---- acquire (raster runner) -------------------------------------------------------


@dataclass
class MapRunState:
    """What the raster runner is doing (lives on ``app.state.mapping``)."""

    active_map_id: int | None = None
    note: str = ""
    cells_total: int = 0
    cells_done: int = 0
    notes: dict[int, str] = field(default_factory=dict)


def _axis_centres(center: float, extent: float, step: float) -> list[float]:
    n = max(1, int(round(extent / step)) + 1)
    return [center + (i - (n - 1) / 2.0) * step for i in range(n)]


def map_cells(sky_map: SkyMap) -> list[tuple[float, float]]:
    """The grid's cell centres as ``(x, y)`` in the map's frame."""
    xs = _axis_centres(sky_map.center_x_deg, sky_map.extent_x_deg, sky_map.step_deg)
    ys = _axis_centres(sky_map.center_y_deg, sky_map.extent_y_deg, sky_map.step_deg)
    return [(x, y) for y in ys for x in xs]


def next_map_cell(
    cells: list[tuple[float, float]],
    done: list[tuple[float, float]],
    station: Station,
) -> tuple[float, float] | None:
    """The next in-limits az/el cell that has no capture yet, or ``None``.

    ``cells`` and ``done`` are az/el pairs (the runner only rasters az/el maps).
    A cell is skipped if it lies outside the station's slew envelope or already
    has an observed capture within :data:`_CELL_MATCH_DEG`.
    """
    for az, el in cells:
        if not station_allows(station, az, el):
            continue
        if any(
            abs(az - daz) <= _CELL_MATCH_DEG and abs(el - dae) <= _CELL_MATCH_DEG
            for daz, dae in done
        ):
            continue
        return az, el
    return None


def start_map(app: FastAPI, map_id: int, cells_total: int) -> None:
    """Begin rastering a map (fresh runner state)."""
    app.state.mapping = MapRunState(
        active_map_id=map_id, note="raster started", cells_total=cells_total
    )


def stop_map(app: FastAPI) -> None:
    """Stop the raster (idempotent)."""
    state: MapRunState = app.state.mapping
    state.active_map_id = None
    state.note = "raster stopped"


def _observed_cells(session: Session, map_id: int) -> list[tuple[float, float]]:
    rows = session.exec(
        select(Capture).where(
            Capture.sky_map_id == map_id,
            col(Capture.map_az_deg).is_not(None),
            col(Capture.map_el_deg).is_not(None),
        )
    ).all()
    return [(float(r.map_az_deg), float(r.map_el_deg)) for r in rows]  # type: ignore[arg-type]


def _ctl(app: FastAPI, request: dict[str, Any]) -> dict[str, Any]:
    return ctl_request(app.state.settings.ctl_endpoint, request, timeout_ms=CTL_TIMEOUT_MS)


async def map_tick(app: FastAPI) -> None:
    """Advance the raster by one cell: slew, dwell-capture, tag it into the map.

    Runs only for an ``active`` az/el map with a running observation-free SDR
    (nothing else capturing). Out-of-limits cells are skipped by
    :func:`next_map_cell`; a full grid or a rotator/transport error stops the
    raster (marking the map ``done``). Best-effort: never raises into the loop.
    """
    state: MapRunState = app.state.mapping
    engine = app.state.engine
    if state.active_map_id is None or engine is None:
        return

    status = _ctl(app, {"cmd": "status"})
    if bool(status.get("ok")) and bool(status.get("capturing")):
        return  # another capture owns the SDR — wait

    with Session(engine) as session:
        sky_map = session.get(SkyMap, state.active_map_id)
        if sky_map is None or sky_map.status != "active" or sky_map.frame != "azel":
            state.note = "raster stopped (map not active / not an az/el map)"
            state.active_map_id = None
            return
        station = session.exec(select(Station)).first()
        location = session.exec(select(Location).where(Location.is_default == True)).first()  # noqa: E712
        if station is None or location is None or sky_map.id is None:
            state.note = "raster stopped (no station/location)"
            state.active_map_id = None
            return
        cells = map_cells(sky_map)
        done = _observed_cells(session, sky_map.id)
        state.cells_total = len(cells)
        state.cells_done = len(done)
        target = next_map_cell(cells, done, station)
        if target is None:
            sky_map.status = "done"
            session.add(sky_map)
            session.commit()
            state.note = f"raster complete ({len(done)}/{len(cells)} cells)"
            state.active_map_id = None
            return
        az, el = target
        map_id, dwell_s = sky_map.id, float(sky_map.dwell_s)

        # Disk guardrail (reuse the scheduler's projection + red threshold).
        from jansky_observe.server.scheduler import projected_capture_bytes, would_exceed_disk_red

        sample_rate = _sample_rate(app)
        projected = projected_capture_bytes("npz", dwell_s / 60.0, sample_rate)
        usage = shutil.disk_usage(app.state.settings.data_dir)
        if would_exceed_disk_red(usage.free, usage.total, projected):
            state.notes[map_id or 0] = "raster paused: would fill the disk past red"
            return

        try:
            await asyncio.to_thread(slew, station, az, el)
        except SlewOutOfLimits:
            return  # next_map_cell already filters these; belt-and-suspenders skip
        except (RotatorUnconfigured, RotatorError) as exc:
            state.note = f"raster stopped (rotator error: {exc})"
            state.active_map_id = None
            return

    # Dwell: record one capture at this cell, then tag it into the map.
    start_reply = _ctl(app, {"cmd": "start_capture", "format": "npz"})
    if not start_reply.get("ok"):
        state.notes[map_id or 0] = f"daemon refused start: {start_reply.get('error')}"
        return
    await asyncio.sleep(max(dwell_s, 0.0))
    stop_reply = _ctl(app, {"cmd": "stop_capture"})
    from jansky_observe.server.routers.captures import register_stopped_capture

    capture_id = await asyncio.to_thread(register_stopped_capture, engine, stop_reply)
    if capture_id is not None:
        await asyncio.to_thread(_tag_capture, engine, capture_id, map_id, az, el)
        state.cells_done += 1
        state.note = f"rastered az {az:.0f}°/el {el:.0f}° ({state.cells_done}/{state.cells_total})"


def _sample_rate(app: FastAPI) -> float:
    latest = app.state.broadcaster.latest
    return float(latest.sample_rate_hz) if latest is not None else 3_000_000.0


def _tag_capture(engine: Any, capture_id: int, map_id: int | None, az: float, el: float) -> None:
    with Session(engine) as session:
        capture = session.get(Capture, capture_id)
        if capture is None:
            return
        capture.sky_map_id = map_id
        capture.map_az_deg = az
        capture.map_el_deg = el
        session.add(capture)
        session.commit()


async def map_loop(app: FastAPI) -> None:
    """Lifespan task: tick the raster forever (best-effort; never crashes)."""
    logger.info("sky-map raster loop started (tick %.0fs)", MAP_TICK_S)
    while True:
        await asyncio.sleep(MAP_TICK_S)
        with contextlib.suppress(Exception):
            await map_tick(app)
