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
from uuid import uuid4

from sqlalchemy import JSON, Column
from sqlmodel import Field, SQLModel

__all__ = [
    "CALIBRATION_KINDS",
    "CAPTURE_KINDS",
    "CAPTURE_POSITIONS",
    "ROTATOR_KINDS",
    "CalibrationEpoch",
    "Campaign",
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
    "Schedule",
    "Station",
    "new_station_uuid",
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


#: Allowed :attr:`Station.rotator_kind` values (roadmap M9). ``none`` = manual;
#: ``sim`` = in-process simulator; ``rotctl`` = rotctld NET protocol over TCP;
#: ``easycomm`` = EasyComm II over USB-serial.
ROTATOR_KINDS: tuple[str, ...] = ("none", "sim", "rotctl", "easycomm")


def new_station_uuid() -> str:
    """Return a fresh random station identifier as a UUID4 string.

    Used as the default for :attr:`Station.uuid` — generated once when the
    station row is created (seed or migration 10) and never changed.
    """
    return str(uuid4())


class Station(SQLModel, table=True):
    """A telescope station — one row for now, a table so a second is trivial.

    ``pointing_offset_az_deg`` / ``pointing_offset_el_deg`` are the Δaz/Δel
    pointing model written by the Sun pointing calibration (plan §5.4) and
    applied to every future pointing display.
    """

    __tablename__ = "station"

    id: int | None = Field(default=None, primary_key=True)
    #: Stable machine identity (UUID4 string), generated once at creation and
    #: never changed — distinct from the editable, human ``name``. Stamped into
    #: the PDF report, the observation bundle, and the MCP identity response so a
    #: spectrum can be traced back to the station that produced it (roadmap M8,
    #: jansky-research plan 78). Backfilled on existing stations by migration 10.
    uuid: str = Field(default_factory=new_station_uuid, index=True)
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
    #: Base URL of a desktop Stellarium's RemoteControl API (plan §4.3),
    #: e.g. ``"http://desktop:8090"``; ``None`` disables the integration.
    stellarium_url: str | None = None
    #: Az/el rotator config (roadmap M9 — the KrakenRF Discovery Drive). ``kind``
    #: is one of :data:`ROTATOR_KINDS`: ``"none"`` (manual, the default),
    #: ``"sim"`` (in-process simulator), ``"rotctl"`` (rotctld NET protocol at
    #: ``rotator_host``:``rotator_port``), or ``"easycomm"`` (EasyComm II over the
    #: serial device ``rotator_serial`` at ``rotator_baud``). The az/el min/max are
    #: hard slew limits enforced before every move; ``park_az_deg``/``park_el_deg``
    #: is the stow pointing (default straight up, el 90°).
    rotator_kind: str = "none"
    rotator_host: str = ""
    rotator_port: int = 4533
    rotator_serial: str = ""
    rotator_baud: int = 19200
    az_min_deg: float = 0.0
    az_max_deg: float = 360.0
    el_min_deg: float = 0.0
    el_max_deg: float = 90.0
    park_az_deg: float = 0.0
    park_el_deg: float = 90.0
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


class Schedule(SQLModel, table=True):
    """An unattended capture schedule (roadmap M7, plans 79/84).

    Fires a capture that starts ``lead_min`` before the source's transit and
    runs ``run_min``. ``repeat`` is ``"once"`` (disabled after firing) or
    ``"daily"``. The server's scheduler loop is the trigger; the capture daemon
    stays the only SDR owner. ``last_run_at`` marks the last window fired so a
    window fires once.
    """

    __tablename__ = "schedule"

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(index=True)
    source_id: int = Field(foreign_key="radio_source.id")
    #: Start this many minutes before the source's transit.
    lead_min: float = 5.0
    #: Run for this many minutes.
    run_min: float = 30.0
    #: Capture format — ``"npz"`` or ``"sigmf"``.
    format: str = "npz"
    #: ``"once"`` or ``"daily"``.
    repeat: str = "daily"
    enabled: bool = True
    #: When the most recent window fired (its window start), or ``None``.
    last_run_at: datetime | None = None
    created_at: datetime = Field(default_factory=utcnow)


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
    #: Soft-delete timestamp (roadmap M6). ``None`` = active; a value hides the
    #: observation from the default lists and the MCP surface, but the row and
    #: all its provenance stay — restorable, never destroyed. HTML-only.
    archived_at: datetime | None = None
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


#: Capture measurement kinds (roadmap M7). ``science`` is the default; the rest
#: are calibration captures grouped under a :class:`CalibrationEpoch`.
CAPTURE_KINDS: tuple[str, ...] = ("science", "ref_load", "cold_sky", "hot_ground")
#: The calibration kinds (everything but ``science``).
CALIBRATION_KINDS: tuple[str, ...] = ("ref_load", "cold_sky", "hot_ground")
#: Position-switch roles (roadmap M10). ``"on"`` (the default) points at the
#: source; ``"off"`` is a nearby blank-sky reference for ON−OFF differencing.
#: Orthogonal to :data:`CAPTURE_KINDS` — an ON or OFF pointing is still a
#: ``science`` capture.
CAPTURE_POSITIONS: tuple[str, ...] = ("on", "off")


class CalibrationEpoch(SQLModel, table=True):
    """A calibration event (roadmap M7, plans 78/79): "as of ``started_at`` the
    receiver was freshly calibrated." Groups the ref-load / cold-sky / hot-ground
    calibration :class:`Capture` rows, and every *science* capture records which
    epoch it falls under — plan 79's weekly cadence as a first-class object, not
    a filename convention.
    """

    __tablename__ = "calibration_epoch"

    id: int | None = Field(default=None, primary_key=True)
    started_at: datetime = Field(default_factory=utcnow)
    notes: str = ""
    created_at: datetime = Field(default_factory=utcnow)


class Campaign(SQLModel, table=True):
    """A drift-scan campaign (roadmap M7, plan 80): fixed-pointing continuous
    capture over many nights. Its captures are tagged with a sidereal-day number
    so passes at the same LST across days stack. The dish sits at a fixed az/el
    (``fixed_az_deg`` / ``fixed_el_deg``) and the sky drifts through the beam.
    """

    __tablename__ = "campaign"

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(index=True)
    source_id: int = Field(foreign_key="radio_source.id")
    fixed_az_deg: float | None = None
    fixed_el_deg: float | None = None
    #: ``"active"`` (captures are tagged into it) or ``"done"``.
    status: str = "active"
    notes: str = ""
    created_at: datetime = Field(default_factory=utcnow)


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
    #: When the on-disk file(s) were purged to reclaim space (roadmap M6). The
    #: row, its settings, and any ClassifierResult provenance survive; only the
    #: bytes are gone. ``None`` = files present. HTML-only.
    purged_at: datetime | None = None
    #: What this capture measures (roadmap M7): ``"science"`` (default) or a
    #: calibration kind — ``"ref_load"`` (50 Ω), ``"cold_sky"``, ``"hot_ground"``.
    kind: str = "science"
    #: The calibration epoch this capture belongs to (a calibration capture) or
    #: was taken under (a science capture, stamped at registration). See
    #: :class:`CalibrationEpoch`.
    cal_epoch_id: int | None = Field(default=None, foreign_key="calibration_epoch.id", index=True)
    #: The drift-scan campaign this capture belongs to (roadmap M7), or ``None``.
    campaign_id: int | None = Field(default=None, foreign_key="campaign.id", index=True)
    #: Integer sidereal-day number at the capture's start (astro
    #: ``sidereal_day_number``) — the drift-scan pass tag; captures on different
    #: sidereal days at the same LST stack. ``None`` outside a campaign.
    sidereal_day: int | None = None
    #: Position-switch role (roadmap M10): ``"on"`` (default, points at the
    #: source) or ``"off"`` (a blank-sky reference). See :data:`CAPTURE_POSITIONS`.
    position: str = "on"
    #: On an ``"off"`` capture, the ``"on"`` :class:`Capture` it references for
    #: ON−OFF differencing (same observation); ``None`` on unpaired captures.
    pair_capture_id: int | None = Field(default=None, foreign_key="capture.id", index=True)
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
