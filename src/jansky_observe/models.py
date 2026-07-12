"""SQLModel entities for observation records (plan §3).

SQLite via SQLModel. Files (IQ, spectra, photos, PDFs) live on disk under
``data/`` and are referenced by path — never blobs in the database. JSON
columns use the SQLAlchemy ``JSON`` type, which SQLite stores as TEXT.

All datetimes are UTC. String "enum" fields (``status``, ``kind``, ``format``,
``source``, ``verdict``, ``mode``) are plain ``str`` columns; the allowed
values are documented on each field and enforced at the API layer.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, Column
from sqlmodel import Field, SQLModel

__all__ = [
    "Capture",
    "ChecklistItemState",
    "ChecklistTemplateItem",
    "ClassifierResult",
    "Location",
    "Observation",
    "ObservationObserver",
    "ObservationType",
    "Observer",
    "Photo",
    "RadioSource",
    "Station",
    "utcnow",
]


def utcnow() -> datetime:
    """Return the current UTC time (timezone-aware).

    Returns
    -------
    datetime
        ``datetime.now(UTC)``. SQLite stores it as a naive ISO string, so
        values read back from the database are naive-UTC by convention.
    """
    return datetime.now(UTC)


class Station(SQLModel, table=True):
    """A telescope station — one row for now, a table so a second is trivial.

    ``pointing_offset_az_deg`` / ``pointing_offset_el_deg`` are the Δaz/Δel
    pointing model written by the Sun pointing calibration (plan §5.4) and
    applied to every future pointing display.
    """

    __tablename__ = "station"

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(unique=True, index=True)
    dish_diameter_m: float
    dish_f_d: float
    feed: str = ""
    #: Ordered RF-chain component names (feed → injector → SDR → …), rendered
    #: as a mermaid diagram in the UI.
    rf_chain: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    compute: str = ""
    mount_type: str = "manual alt-az"
    notes: str = ""
    #: Δaz from the Sun pointing calibration (scale reading minus prediction).
    pointing_offset_az_deg: float = 0.0
    #: Δel from the Sun pointing calibration (gauge reading minus prediction).
    pointing_offset_el_deg: float = 0.0
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class Location(SQLModel, table=True):
    """A named observing location; "Home" is the default. Geocode once, store forever."""

    __tablename__ = "location"

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(unique=True, index=True)
    lat_deg: float
    lon_deg: float
    elevation_m: float
    #: One of ``"geocoded" | "gps" | "manual"``.
    source: str = "manual"
    address: str = ""
    is_default: bool = False
    created_at: datetime = Field(default_factory=utcnow)


class Observer(SQLModel, table=True):
    """A person (or Claude) who participates in observations."""

    __tablename__ = "observer"

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(unique=True, index=True)
    callsign: str = ""
    email: str = ""
    notes: str = ""
    created_at: datetime = Field(default_factory=utcnow)


class RadioSource(SQLModel, table=True):
    """A target on the sky.

    Point sources carry RA/Dec; galactic HI targets may carry l/b instead;
    the Sun carries neither (its ephemeris is computed live with astropy).
    """

    __tablename__ = "radio_source"

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(unique=True, index=True)
    #: One of ``"hi_region" | "point_source" | "sun" | "custom"``.
    kind: str
    ra_deg: float | None = None
    dec_deg: float | None = None
    gal_l_deg: float | None = None
    gal_b_deg: float | None = None
    notes: str = ""
    created_at: datetime = Field(default_factory=utcnow)


class ObservationType(SQLModel, table=True):
    """A procedure template: default SDR settings + a checklist (plan §5.4)."""

    __tablename__ = "observation_type"

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(unique=True, index=True)
    description: str = ""
    #: Default SDR settings applied when the session wizard picks this type
    #: (e.g. ``{"center_freq_hz": 1420.4e6, "sample_rate_hz": 3e6, "gain": 16}``).
    default_sdr_settings: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utcnow)


class ChecklistTemplateItem(SQLModel, table=True):
    """One ordered checklist line of an :class:`ObservationType` template."""

    __tablename__ = "checklist_template_item"

    id: int | None = Field(default=None, primary_key=True)
    observation_type_id: int = Field(foreign_key="observation_type.id", index=True)
    order_index: int
    text: str
    #: ``True`` = required, ``False`` = recommended.
    required: bool = True


class Observation(SQLModel, table=True):
    """One attended observing session, planned or performed."""

    __tablename__ = "observation"

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(index=True)
    observation_type_id: int = Field(foreign_key="observation_type.id")
    station_id: int = Field(foreign_key="station.id")
    location_id: int = Field(foreign_key="location.id")
    source_id: int = Field(foreign_key="radio_source.id")
    planned_start: datetime | None = None
    planned_end: datetime | None = None
    actual_start: datetime | None = None
    actual_end: datetime | None = None
    #: One of ``"planned" | "running" | "done" | "aborted"``.
    status: str = "planned"
    #: NWS snapshot captured at start: temp, wind, sky cover, humidity.
    weather_snapshot: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))
    #: What was dialed onto the mast scale / angle gauge by hand.
    pointing_az_deg: float | None = None
    pointing_el_deg: float | None = None
    #: What astropy computed for the source at actual start.
    computed_az_deg: float | None = None
    computed_el_deg: float | None = None
    #: Markdown session notes.
    notes: str = ""
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class ObservationObserver(SQLModel, table=True):
    """Link table: which observers participated in which observation (M2M)."""

    __tablename__ = "observation_observer"

    observation_id: int = Field(foreign_key="observation.id", primary_key=True)
    observer_id: int = Field(foreign_key="observer.id", primary_key=True)


class ChecklistItemState(SQLModel, table=True):
    """Persisted per-observation checklist state — exactly what was performed."""

    __tablename__ = "checklist_item_state"

    id: int | None = Field(default=None, primary_key=True)
    observation_id: int = Field(foreign_key="observation.id", index=True)
    template_item_id: int = Field(foreign_key="checklist_template_item.id")
    checked: bool = False
    checked_at: datetime | None = None
    #: Observer name, or ``"claude"`` (provenance rule, plan §12.5).
    checked_by: str = ""


class Capture(SQLModel, table=True):
    """A recorded data file on disk, referenced by path.

    ``observation_id`` is nullable: M1 capture files predate observations.
    """

    __tablename__ = "capture"

    id: int | None = Field(default=None, primary_key=True)
    observation_id: int | None = Field(default=None, foreign_key="observation.id", index=True)
    #: e.g. ``"airspy" | "hackrf"``.
    device: str
    path: str
    #: One of ``"sigmf" | "npz_spectra" | "hackrf_sweep_csv"``.
    format: str
    size_bytes: int = 0
    start: datetime | None = None
    end: datetime | None = None
    #: Full SDR settings: center freq, sample rate, gains, bias-tee state,
    #: FFT size, integration time.
    sdr_settings: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utcnow)


class Photo(SQLModel, table=True):
    """A photo attached to an observation (ingest/resize workflow lands at M4)."""

    __tablename__ = "photo"

    id: int | None = Field(default=None, primary_key=True)
    observation_id: int = Field(foreign_key="observation.id", index=True)
    path: str
    caption: str = ""
    #: Exactly one highlight per observation; shown large on the PDF.
    is_highlight: bool = False
    taken_at: datetime | None = None
    created_at: datetime = Field(default_factory=utcnow)


class ClassifierResult(SQLModel, table=True):
    """A confirmation verdict for a capture (classifiers land at M3)."""

    __tablename__ = "classifier_result"

    id: int | None = Field(default=None, primary_key=True)
    capture_id: int = Field(foreign_key="capture.id", index=True)
    name: str
    version: str
    #: One of ``"detected" | "not_detected" | "uncertain"``.
    verdict: str
    score: float = 0.0
    #: e.g. measured peak frequency, SNR, fit parameters.
    params: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    #: One of ``"live" | "post"``.
    mode: str = "post"
    created_at: datetime = Field(default_factory=utcnow)
