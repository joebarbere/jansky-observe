"""Codified observation bundle (roadmap M8, jansky-research plan 78).

One documented, machine-recoverable export per observation: a manifest of
provenance — station UUID, pointing, LST, timestamps, gain settings, cal-epoch
reference, and classifier verdicts — plus one averaged-spectrum ``.npz`` per
``npz_spectra`` capture. This is exactly the "averaged-spectra format from the
station's capture service" plan 78 consumes.

Two entry points:

- :func:`build_observation_manifest` — the JSON block (a plain dict). It is what
  ``GET /api/observations/{id}/bundle.json`` and the ``get_observation_bundle``
  MCP tool return, and what the PDF report embeds so a report alone is
  machine-recoverable.
- :func:`write_observation_bundle` — writes ``bundle.json`` + ``capture-<id>.npz``
  files and zips them to ``<out_dir>/observation-<id>-bundle.zip``.

:data:`BUNDLE_SCHEMA` identifies the format; bump it on any breaking change.
"""

from __future__ import annotations

import json
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
from sqlmodel import Session, col, select

from jansky_observe import __version__
from jansky_observe.astro.pointing import local_sidereal_time_hours
from jansky_observe.confirm.classifier import averaged_spectrum
from jansky_observe.confirm.radiometer import radiometer_estimate
from jansky_observe.models import (
    CalibrationEpoch,
    Capture,
    ClassifierResult,
    Location,
    Observation,
    ObservationObserver,
    ObservationType,
    Observer,
    RadioSource,
    SkyMap,
    Station,
    utcnow,
)

__all__ = [
    "BUNDLE_SCHEMA",
    "build_observation_manifest",
    "write_observation_bundle",
]

#: Format identifier for the observation bundle. Bump on any breaking change.
BUNDLE_SCHEMA = "jansky-observe.observation-bundle/1"


def _iso(value: datetime | None) -> str | None:
    """A datetime as an ISO-8601 string (UTC by convention), or ``None``."""
    return value.isoformat() if value is not None else None


def _float_or_nan(value: Any) -> float:
    """A JSON/np-safe float — ``NaN`` for anything not coercible (missing key)."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return float("nan")


def _capture_has_spectrum(capture: Capture) -> bool:
    """True when the capture is an averaged-spectrum ``.npz`` still on disk."""
    return (
        capture.format == "npz_spectra"
        and capture.purged_at is None
        and Path(capture.path).is_file()
    )


def _radiometer_block(
    capture: Capture, cal_epoch: CalibrationEpoch | None
) -> dict[str, Any] | None:
    """The radiometer estimate for a capture (roadmap M12), or ``None``.

    Best-effort provenance: needs an M10 Tsys on the epoch and a readable npz for
    the bandwidth/integration; any gap → ``None`` (never raises into the manifest).
    An estimate, not a verdict."""
    if cal_epoch is None or cal_epoch.tsys_k is None or not _capture_has_spectrum(capture):
        return None
    try:
        with np.load(Path(capture.path), allow_pickle=False) as data:
            power_db = np.asarray(data["power_db"], dtype=np.float64)
            sample_rate_hz = float(data["sample_rate_hz"])
            timestamps = np.asarray(data.get("timestamps", []), dtype=np.float64)
        n_fft = power_db.shape[1] if power_db.ndim == 2 else power_db.shape[0]
        integration_s = float(timestamps[-1] - timestamps[0]) if timestamps.size > 1 else 0.0
        if integration_s <= 0.0 and capture.start is not None and capture.end is not None:
            integration_s = max((capture.end - capture.start).total_seconds(), 0.0)
        if integration_s <= 0.0:
            return None
        return radiometer_estimate(
            tsys_k=cal_epoch.tsys_k,
            channel_bw_hz=sample_rate_hz / n_fft,
            integration_s=integration_s,
        )
    except Exception:  # noqa: BLE001 — provenance is best-effort; degrade to None
        return None


def _capture_block(
    session: Session, capture: Capture, lon_deg: float, az_el: tuple[float | None, float | None]
) -> dict[str, Any]:
    """The manifest entry for one capture: settings, LST, cal-epoch, verdicts."""
    lst_hours = (
        local_sidereal_time_hours(lon_deg, capture.start) if capture.start is not None else None
    )
    cal_epoch = (
        session.get(CalibrationEpoch, capture.cal_epoch_id)
        if capture.cal_epoch_id is not None
        else None
    )
    results = session.exec(
        select(ClassifierResult)
        .where(ClassifierResult.capture_id == capture.id)
        .order_by(col(ClassifierResult.id))
    ).all()
    az_deg, el_deg = az_el
    return {
        "id": capture.id,
        "device": capture.device,
        "format": capture.format,
        "kind": capture.kind,
        "start": _iso(capture.start),
        "end": _iso(capture.end),
        "size_bytes": capture.size_bytes,
        "purged": capture.purged_at is not None,
        "sdr_settings": capture.sdr_settings,
        "az_deg": az_deg,
        "el_deg": el_deg,
        "lst_hours_at_start": lst_hours,
        "cal_epoch": (
            {"id": cal_epoch.id, "started_at": _iso(cal_epoch.started_at)}
            if cal_epoch is not None
            else None
        ),
        # Radiometer-equation estimate (roadmap M12) when Tsys is available — an
        # estimate that contextualises the empirical classifier SNR, not a verdict.
        "radiometer": _radiometer_block(capture, cal_epoch),
        "campaign_id": capture.campaign_id,
        "sidereal_day": capture.sidereal_day,
        "classifier_results": [
            {
                "name": r.name,
                "version": r.version,
                "verdict": r.verdict,
                "score": r.score,
                "mode": r.mode,
                "params": r.params,
                "created_at": _iso(r.created_at),
            }
            for r in results
        ],
        # Present only for on-disk npz captures; names the file written into the
        # zip (absent for SigMF, sweeps, or purged captures).
        "spectrum_file": (f"capture-{capture.id}.npz" if _capture_has_spectrum(capture) else None),
    }


def build_observation_manifest(session: Session, observation: Observation) -> dict[str, Any]:
    """Assemble the observation bundle manifest (plan 78's input format).

    Parameters
    ----------
    session : Session
        An open session.
    observation : Observation
        The observation to bundle.

    Returns
    -------
    dict
        A JSON-serializable manifest: schema id, station identity (with the
        stable UUID), location, the observation with its pointing, and a per-
        capture block carrying settings, LST, cal-epoch, and classifier verdicts.
    """
    station = session.get(Station, observation.station_id)
    location = session.get(Location, observation.location_id)
    source = session.get(RadioSource, observation.source_id)
    obs_type = session.get(ObservationType, observation.observation_type_id)
    observers = session.exec(
        select(Observer)
        .where(ObservationObserver.observation_id == observation.id)
        .where(ObservationObserver.observer_id == Observer.id)
        .order_by(col(Observer.name))
    ).all()
    captures = session.exec(
        select(Capture).where(Capture.observation_id == observation.id).order_by(col(Capture.id))
    ).all()
    # The distinct HI sky maps (roadmap M11) this observation's captures feed —
    # light provenance (geometry + metric); the gridded values ride the map API.
    sky_map_ids = sorted({c.sky_map_id for c in captures if c.sky_map_id is not None})
    sky_maps = [session.get(SkyMap, mid) for mid in sky_map_ids]

    az_deg = (
        observation.pointing_az_deg
        if observation.pointing_az_deg is not None
        else observation.computed_az_deg
    )
    el_deg = (
        observation.pointing_el_deg
        if observation.pointing_el_deg is not None
        else observation.computed_el_deg
    )
    lon_deg = location.lon_deg if location is not None else 0.0

    return {
        "schema": BUNDLE_SCHEMA,
        "generated_at": _iso(utcnow()),
        "software_version": __version__,
        "station": (
            {
                "uuid": station.uuid,
                "name": station.name,
                "dish_diameter_m": station.dish_diameter_m,
                "dish_f_d": station.dish_f_d,
                "mount_type": station.mount_type,
            }
            if station is not None
            else None
        ),
        "location": (
            {
                "name": location.name,
                "lat_deg": location.lat_deg,
                "lon_deg": location.lon_deg,
                "elevation_m": location.elevation_m,
            }
            if location is not None
            else None
        ),
        "observation": {
            "id": observation.id,
            "name": observation.name,
            "status": observation.status,
            "type": obs_type.name if obs_type is not None else None,
            "source": (
                {
                    "name": source.name,
                    "kind": source.kind,
                    "ra_deg": source.ra_deg,
                    "dec_deg": source.dec_deg,
                    "gal_l_deg": source.gal_l_deg,
                    "gal_b_deg": source.gal_b_deg,
                }
                if source is not None
                else None
            ),
            "planned_start": _iso(observation.planned_start),
            "planned_end": _iso(observation.planned_end),
            "actual_start": _iso(observation.actual_start),
            "actual_end": _iso(observation.actual_end),
            "pointing": {
                "dialed_az_deg": observation.pointing_az_deg,
                "dialed_el_deg": observation.pointing_el_deg,
                "computed_az_deg": observation.computed_az_deg,
                "computed_el_deg": observation.computed_el_deg,
            },
            "observers": [o.name for o in observers],
        },
        "captures": [_capture_block(session, c, lon_deg, (az_deg, el_deg)) for c in captures],
        "sky_maps": [
            {
                "id": m.id,
                "name": m.name,
                "frame": m.frame,
                "metric": m.metric,
                "center_deg": [m.center_x_deg, m.center_y_deg],
                "extent_deg": [m.extent_x_deg, m.extent_y_deg],
                "step_deg": m.step_deg,
                "status": m.status,
            }
            for m in sky_maps
            if m is not None
        ],
    }


def _write_capture_npz(
    capture: Capture, block: dict[str, Any], station_uuid: str, out: Path
) -> None:
    """Write one self-describing averaged-spectrum ``.npz`` (pickle-free).

    Carries the averaged spectrum plus the scalar provenance a consumer needs
    to use the file standalone — pointing, LST, and the key SDR settings — so
    plan 78 can read a single npz without the manifest.
    """
    freq_hz, power_db = averaged_spectrum(capture.path)
    settings = capture.sdr_settings or {}
    np.savez(
        out,
        frequency_hz=freq_hz,
        power_db=power_db,
        capture_id=np.int64(capture.id if capture.id is not None else -1),
        observation_id=np.int64(
            capture.observation_id if capture.observation_id is not None else -1
        ),
        station_uuid=station_uuid,
        kind=capture.kind,
        start_utc=block["start"] or "",
        az_deg=_float_or_nan(block["az_deg"]),
        el_deg=_float_or_nan(block["el_deg"]),
        lst_hours=_float_or_nan(block["lst_hours_at_start"]),
        center_freq_hz=_float_or_nan(settings.get("center_freq_hz")),
        sample_rate_hz=_float_or_nan(settings.get("sample_rate_hz")),
        gain=_float_or_nan(settings.get("gain")),
    )


def write_observation_bundle(
    session: Session, observation: Observation, out_dir: str | Path
) -> Path:
    """Write the observation bundle (manifest + per-capture npz) as one zip.

    Parameters
    ----------
    session : Session
        An open session.
    observation : Observation
        The observation to bundle.
    out_dir : str or Path
        Directory for the zip (created if missing); overwritten on re-export.

    Returns
    -------
    Path
        ``<out_dir>/observation-<id>-bundle.zip`` — ``bundle.json`` plus one
        ``capture-<id>.npz`` for each on-disk averaged-spectrum capture.
    """
    manifest = build_observation_manifest(session, observation)
    station_uuid = str(manifest["station"]["uuid"]) if manifest["station"] else ""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    zip_path = out_dir / f"observation-{observation.id}-bundle.zip"

    captures = {
        c.id: c
        for c in session.exec(select(Capture).where(Capture.observation_id == observation.id)).all()
    }
    staging = out_dir / f".observation-{observation.id}-npz"
    staging.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("bundle.json", json.dumps(manifest, indent=2))
            for block in manifest["captures"]:
                if block["spectrum_file"] is None:
                    continue
                capture = captures[block["id"]]
                npz_path = staging / block["spectrum_file"]
                _write_capture_npz(capture, block, station_uuid, npz_path)
                archive.write(npz_path, block["spectrum_file"])
    finally:
        for leftover in staging.glob("*.npz"):
            leftover.unlink()
        staging.rmdir()
    return zip_path
