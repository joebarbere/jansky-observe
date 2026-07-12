"""The observation PDF report: gather → figures → Jinja2 → WeasyPrint (plan §7).

One templating system: ``templates/report.html`` is a print-CSS variant of
the web observation page, rendered with plain Jinja2 (no request needed) and
turned into ``<data_dir>/observations/<id>/report.pdf`` by WeasyPrint.
Figures are regenerated and the PDF overwritten on every rebuild — the
report is a view of the database, never a source of truth.

Contents (plan §7): header (name, date, station, observers) · highlight
photo large · metadata block (source, RA/Dec + az/el at start, times,
location, SDR settings) · weather snapshot · checklist as performed ·
integrated profile + waterfall per capture (dual MHz/v_LSR axes when the
pointing is known, §4.6) · classifier verdicts · notes · additional photos
(grid) · capture inventory. Everything degrades gracefully — an observation
with no photos, no captures, or missing files still renders.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from astropy.coordinates import SkyCoord
from jinja2 import Environment, FileSystemLoader
from sqlalchemy import Engine
from sqlmodel import Session, col, select

from jansky_observe import __version__
from jansky_observe.astro.pointing import target_coord
from jansky_observe.export.figures import profile_figure, waterfall_figure
from jansky_observe.models import (
    Capture,
    ChecklistItemState,
    ChecklistTemplateItem,
    ClassifierResult,
    Location,
    Observation,
    ObservationObserver,
    ObservationType,
    Observer,
    Photo,
    RadioSource,
    Station,
    utcnow,
)

__all__ = ["build_report", "report_path"]


def report_path(data_dir: str | Path, observation_id: int) -> Path:
    """Where an observation's report PDF lives under the data directory."""
    return Path(data_dir) / "observations" / str(observation_id) / "report.pdf"


def _fmt_dt(value: datetime | None) -> str:
    """Render a (naive-UTC-by-convention) datetime as ``YYYY-MM-DD HH:MM UTC``."""
    if value is None:
        return "—"
    return value.strftime("%Y-%m-%d %H:%M UTC")


def _fmt_size(size_bytes: int) -> str:
    """Human-readable file size (MB below 1 GB, GB above)."""
    if size_bytes >= 1e9:
        return f"{size_bytes / 1e9:.2f} GB"
    return f"{size_bytes / 1e6:.1f} MB"


def _source_coord(source: RadioSource, when: datetime | None) -> SkyCoord:
    """Pointing coordinate for a source row (same rule as the live badge)."""
    if source.kind == "sun":
        return target_coord("sun", when=when)
    if source.gal_l_deg is not None and source.gal_b_deg is not None:
        return target_coord("galactic", gal_l_deg=source.gal_l_deg, gal_b_deg=source.gal_b_deg)
    return target_coord("radec", ra_deg=source.ra_deg, dec_deg=source.dec_deg)


def _photo_entries(
    session: Session, observation: Observation, data_dir: Path
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """The highlight photo (or ``None``) and the remaining photos, in order.

    Photos whose files are missing from disk are skipped — the report never
    fails on a lost file.
    """
    rows = session.exec(
        select(Photo).where(Photo.observation_id == observation.id).order_by(col(Photo.id))
    ).all()
    highlight: dict[str, Any] | None = None
    others: list[dict[str, Any]] = []
    for photo in rows:
        path = Path(photo.path)
        if not path.is_absolute():
            path = data_dir / path
        if not path.is_file():
            continue
        entry = {
            "uri": path.resolve().as_uri(),
            "caption": photo.caption,
            "taken_at": photo.taken_at,
        }
        if photo.is_highlight and highlight is None:
            highlight = entry
        else:
            others.append(entry)
    return highlight, others


def _capture_entries(
    session: Session,
    observation: Observation,
    source: RadioSource | None,
    location: Location | None,
    data_dir: Path,
) -> list[dict[str, Any]]:
    """Capture inventory rows with per-capture verdicts and rendered figures.

    For each ``npz_spectra`` capture the profile and waterfall figures are
    (re)generated under ``<data_dir>/observations/<id>/figures/``; the
    profile carries the dual MHz/v_LSR axes when the observation supplies a
    pointing (plan §4.6). Missing capture files degrade to figure-less rows.
    """
    figures_dir = data_dir / "observations" / str(observation.id) / "figures"
    captures = session.exec(
        select(Capture).where(Capture.observation_id == observation.id).order_by(col(Capture.id))
    ).all()
    entries: list[dict[str, Any]] = []
    for capture in captures:
        results = session.exec(
            select(ClassifierResult)
            .where(ClassifierResult.capture_id == capture.id)
            .order_by(col(ClassifierResult.id))
        ).all()
        entry: dict[str, Any] = {
            "capture": capture,
            "filename": Path(capture.path).name,
            "size": _fmt_size(capture.size_bytes),
            "settings_json": json.dumps(capture.sdr_settings, sort_keys=True),
            "results": list(results),
            "profile_uri": None,
            "waterfall_uri": None,
        }
        if capture.format == "npz_spectra" and Path(capture.path).is_file():
            pointing: dict[str, Any] = {}
            if source is not None and location is not None:
                when = capture.start or observation.actual_start or capture.created_at
                pointing = {
                    "coord": _source_coord(source, when),
                    "lat": location.lat_deg,
                    "lon": location.lon_deg,
                    "elev": location.elevation_m,
                    "when": when,
                }
            profile = profile_figure(
                capture.path, figures_dir / f"capture-{capture.id}-profile.png", **pointing
            )
            waterfall = waterfall_figure(
                capture.path, figures_dir / f"capture-{capture.id}-waterfall.png"
            )
            entry["profile_uri"] = profile.resolve().as_uri()
            entry["waterfall_uri"] = waterfall.resolve().as_uri()
        entries.append(entry)
    return entries


def _gather_context(session: Session, observation: Observation, data_dir: Path) -> dict[str, Any]:
    """Everything ``report.html`` renders, from the database + disk."""
    obs_type = session.get(ObservationType, observation.observation_type_id)
    station = session.get(Station, observation.station_id)
    location = session.get(Location, observation.location_id)
    source = session.get(RadioSource, observation.source_id)
    observers = session.exec(
        select(Observer)
        .where(ObservationObserver.observation_id == observation.id)
        .where(ObservationObserver.observer_id == Observer.id)
        .order_by(col(Observer.name))
    ).all()
    checklist = session.exec(
        select(ChecklistItemState, ChecklistTemplateItem)
        .where(ChecklistItemState.observation_id == observation.id)
        .where(ChecklistItemState.template_item_id == ChecklistTemplateItem.id)
        .order_by(col(ChecklistTemplateItem.order_index))
    ).all()
    highlight, photos = _photo_entries(session, observation, data_dir)
    return {
        "obs": observation,
        "obs_type": obs_type,
        "station": station,
        "location": location,
        "source": source,
        "observers": list(observers),
        "checklist": list(checklist),
        "weather": observation.weather_snapshot,
        "highlight": highlight,
        "photos": photos,
        "captures": _capture_entries(session, observation, source, location, data_dir),
        "generated_at": utcnow(),
        "version": __version__,
    }


def build_report(
    engine: Engine,
    observation_id: int,
    data_dir: str | Path,
    templates_dir: str | Path,
) -> Path:
    """Build (or rebuild) an observation's PDF report.

    Parameters
    ----------
    engine : Engine
        The station database engine.
    observation_id : int
        The observation to report on.
    data_dir : str or Path
        The station data directory; figures land under
        ``observations/<id>/figures/`` and the PDF at
        ``observations/<id>/report.pdf`` (overwritten on rebuild).
    templates_dir : str or Path
        Directory containing ``report.html`` (the server's ``templates/``).

    Returns
    -------
    Path
        The written PDF's path.

    Raises
    ------
    LookupError
        If no observation has that id.
    """
    # WeasyPrint is heavyweight (pango/cairo); import at call time so merely
    # importing jansky_observe.export never requires the system libraries.
    from weasyprint import HTML

    data = Path(data_dir)
    with Session(engine) as session:
        observation = session.get(Observation, observation_id)
        if observation is None:
            raise LookupError(f"Observation {observation_id} not found")
        context = _gather_context(session, observation, data)

    env = Environment(loader=FileSystemLoader(str(templates_dir)), autoescape=True)
    env.filters["dt"] = _fmt_dt
    html = env.get_template("report.html").render(**context)

    out = report_path(data, observation_id)
    out.parent.mkdir(parents=True, exist_ok=True)
    HTML(string=html, base_url=str(data)).write_pdf(str(out))
    return out
