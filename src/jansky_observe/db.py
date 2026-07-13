"""SQLite engine + forward-migration-on-start (plan §9: the schema migrates
forward on server start; first release where it matters is M2).

The database is a single file, ``<data_dir>/jansky-observe.sqlite3``. Schema
versions are tracked in SQLite's ``PRAGMA user_version`` (0 on a fresh file).
:data:`MIGRATIONS` is an ordered list of ``(version, apply)`` pairs with
monotonically increasing versions; :func:`migrate` runs, in order, every
migration above the current ``user_version``, setting ``user_version`` after
each, one transaction per migration. A fresh database therefore ends at the
latest version; an old database is walked forward.

Adding a migration (what the ``/new-migration`` skill scaffolds)
----------------------------------------------------------------
Migration 1 creates the full current schema, so later migrations are plain
DDL callables appended to :data:`MIGRATIONS`::

    def _migration_3_station_backlash(conn: Connection) -> None:
        \"\"\"Add the azimuth-backlash column to station.\"\"\"
        conn.exec_driver_sql(
            "ALTER TABLE station ADD COLUMN backlash_az_deg FLOAT NOT NULL DEFAULT 0.0"
        )

    MIGRATIONS.append((3, _migration_3_station_backlash))

Remember to also add the new field to the SQLModel class in ``models.py`` —
``create_all`` in migration 1 always builds the *latest* schema for fresh
databases, and both paths must agree. Because migration 1 builds the latest
schema, a later ALTER TABLE that adds a column present in the latest models
must guard on ``PRAGMA table_info`` (see migration 2): on a fresh database
the column already exists when the migration runs.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from sqlalchemy import Connection, Engine, create_engine
from sqlmodel import Session, SQLModel

from jansky_observe import models  # noqa: F401  # register every table on SQLModel.metadata
from jansky_observe.seeds import seed_all

__all__ = ["DB_FILENAME", "MIGRATIONS", "get_engine", "init_db", "migrate", "session"]

DB_FILENAME = "jansky-observe.sqlite3"


def get_engine(data_dir: str | Path) -> Engine:
    """Create an engine for the SQLite file under ``data_dir``.

    Parameters
    ----------
    data_dir : str or Path
        The station data directory (``Settings.data_dir``); created if missing.

    Returns
    -------
    Engine
        A SQLAlchemy engine for ``<data_dir>/jansky-observe.sqlite3``.
    """
    path = Path(data_dir)
    path.mkdir(parents=True, exist_ok=True)
    return create_engine(f"sqlite:///{path / DB_FILENAME}")


def _migration_1_initial_schema(conn: Connection) -> None:
    """Create every table (the full M2 schema) and insert the seed data."""
    SQLModel.metadata.create_all(conn)
    with Session(conn) as s:
        seed_all(s)


def _migration_2_station_stellarium_url(conn: Connection) -> None:
    """Add ``station.stellarium_url`` — the desktop Stellarium RemoteControl
    base URL for the M5 finder-view integration (plan §4.3).

    Nullable, no default: ``NULL`` means "no Stellarium configured". Guarded
    on ``PRAGMA table_info`` because migration 1 builds the *latest* schema —
    on a fresh database the column already exists when this runs, and both
    paths must land on the identical schema.
    """
    columns = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(station)")}
    if "stellarium_url" not in columns:
        conn.exec_driver_sql("ALTER TABLE station ADD COLUMN stellarium_url VARCHAR")


def _migration_3_observation_archived_at(conn: Connection) -> None:
    """Add ``observation.archived_at`` — the soft-delete timestamp (roadmap M6).

    Nullable, no default: ``NULL`` means "active". Guarded on ``PRAGMA
    table_info`` because migration 1 builds the *latest* schema — on a fresh
    database the column already exists when this runs, and both paths must land
    on the identical schema.
    """
    columns = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(observation)")}
    if "archived_at" not in columns:
        conn.exec_driver_sql("ALTER TABLE observation ADD COLUMN archived_at DATETIME")


def _migration_4_capture_purged_at(conn: Connection) -> None:
    """Add ``capture.purged_at`` — when the on-disk file(s) were reclaimed while
    the row + provenance were kept (roadmap M6). Nullable, no default; guarded
    on ``PRAGMA table_info`` for the same reason as migration 3.
    """
    columns = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(capture)")}
    if "purged_at" not in columns:
        conn.exec_driver_sql("ALTER TABLE capture ADD COLUMN purged_at DATETIME")


def _migration_5_rfi_survey_1420_type(conn: Connection) -> None:
    """Seed the guided "RFI survey @ 1420" ObservationType + its before/after
    checklist (roadmap M6). This is a data-only migration: it re-runs the
    idempotent :func:`seeds.seed_observation_types`, which inserts only the
    ObservationTypes missing by name — so on an existing station it adds just
    the new type, and on a fresh database (where migration 1 already seeded it
    from ``seeds.OBSERVATION_TYPES``) it is a no-op. Mirrors how migration 1
    itself seeds via the models.
    """
    from jansky_observe.seeds import seed_observation_types

    with Session(conn) as session:
        seed_observation_types(session)
        session.commit()


def _migration_6_calibration(conn: Connection) -> None:
    """Add calibration captures (roadmap M7): the ``calibration_epoch`` table
    and ``capture.kind`` / ``capture.cal_epoch_id``.

    The ``CREATE TABLE IF NOT EXISTS`` and the ``PRAGMA table_info`` guards make
    this a no-op on a fresh database (migration 1's ``create_all`` already built
    the latest schema) and additive on an existing one. The DDL is frozen here
    to match the models as of M7; later changes are new migrations.
    """
    conn.exec_driver_sql(
        "CREATE TABLE IF NOT EXISTS calibration_epoch ("
        " id INTEGER NOT NULL PRIMARY KEY,"
        " started_at DATETIME NOT NULL,"
        " notes VARCHAR NOT NULL DEFAULT '',"
        " created_at DATETIME NOT NULL"
        ")"
    )
    columns = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(capture)")}
    if "kind" not in columns:
        conn.exec_driver_sql(
            "ALTER TABLE capture ADD COLUMN kind VARCHAR NOT NULL DEFAULT 'science'"
        )
    if "cal_epoch_id" not in columns:
        conn.exec_driver_sql("ALTER TABLE capture ADD COLUMN cal_epoch_id INTEGER")


def _migration_7_calibration_sweep_type(conn: Connection) -> None:
    """Seed the guided "Calibration sweep" ObservationType (roadmap M7). Re-runs
    the idempotent :func:`seeds.seed_observation_types` (inserts only types
    missing by name) — same pattern as migration 5, and a no-op on a fresh
    database where migration 1 already seeded it."""
    from jansky_observe.seeds import seed_observation_types

    with Session(conn) as session:
        seed_observation_types(session)
        session.commit()


def _migration_8_driftscan(conn: Connection) -> None:
    """Add drift-scan campaigns (roadmap M7, plan 80): the ``campaign`` table and
    ``capture.campaign_id`` / ``capture.sidereal_day``. Frozen DDL matching the
    M7 models; ``IF NOT EXISTS`` + ``PRAGMA table_info`` guards make it a no-op on
    a fresh database and additive on an existing one.
    """
    conn.exec_driver_sql(
        "CREATE TABLE IF NOT EXISTS campaign ("
        " id INTEGER NOT NULL PRIMARY KEY,"
        " name VARCHAR NOT NULL,"
        " source_id INTEGER NOT NULL,"
        " fixed_az_deg FLOAT,"
        " fixed_el_deg FLOAT,"
        " status VARCHAR NOT NULL DEFAULT 'active',"
        " notes VARCHAR NOT NULL DEFAULT '',"
        " created_at DATETIME NOT NULL,"
        " FOREIGN KEY(source_id) REFERENCES radio_source (id)"
        ")"
    )
    columns = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(capture)")}
    if "campaign_id" not in columns:
        conn.exec_driver_sql("ALTER TABLE capture ADD COLUMN campaign_id INTEGER")
    if "sidereal_day" not in columns:
        conn.exec_driver_sql("ALTER TABLE capture ADD COLUMN sidereal_day INTEGER")


def _migration_9_schedule(conn: Connection) -> None:
    """Add the unattended-capture ``schedule`` table (roadmap M7, plans 79/84).
    Frozen DDL matching the M7 model; ``IF NOT EXISTS`` makes it a no-op on a
    fresh database.
    """
    conn.exec_driver_sql(
        "CREATE TABLE IF NOT EXISTS schedule ("
        " id INTEGER NOT NULL PRIMARY KEY,"
        " name VARCHAR NOT NULL,"
        " source_id INTEGER NOT NULL,"
        " lead_min FLOAT NOT NULL DEFAULT 5.0,"
        " run_min FLOAT NOT NULL DEFAULT 30.0,"
        " format VARCHAR NOT NULL DEFAULT 'npz',"
        " repeat VARCHAR NOT NULL DEFAULT 'daily',"
        " enabled BOOLEAN NOT NULL DEFAULT 1,"
        " last_run_at DATETIME,"
        " created_at DATETIME NOT NULL,"
        " FOREIGN KEY(source_id) REFERENCES radio_source (id)"
        ")"
    )


def _migration_10_station_uuid(conn: Connection) -> None:
    """Add ``station.uuid`` — a stable UUID4 station identity (roadmap M8,
    jansky-research plan 78) stamped into the report, the observation bundle, and
    the MCP identity response.

    Guarded on ``PRAGMA table_info`` (migration 1 builds the latest schema, so a
    fresh database already has the column, populated by the model's
    ``default_factory`` at seed time). On an existing station the ``ALTER`` adds
    the column with an empty default; SQLite has no UUID function, so we generate
    in Python and backfill any row still missing one. The ``ALTER`` cannot carry
    the model's ``index=True`` (SQLite forbids indexed/unique columns in
    ``ADD COLUMN``), so we create the matching index separately — both paths land
    on the same schema.
    """
    from uuid import uuid4

    columns = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(station)")}
    if "uuid" not in columns:
        conn.exec_driver_sql("ALTER TABLE station ADD COLUMN uuid VARCHAR NOT NULL DEFAULT ''")
    for (station_id,) in conn.exec_driver_sql(
        "SELECT id FROM station WHERE uuid IS NULL OR uuid = ''"
    ).fetchall():
        conn.exec_driver_sql("UPDATE station SET uuid = ? WHERE id = ?", (str(uuid4()), station_id))
    conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_station_uuid ON station (uuid)")


MIGRATIONS: list[tuple[int, Callable[[Connection], None]]] = [
    (1, _migration_1_initial_schema),
    (2, _migration_2_station_stellarium_url),
    (3, _migration_3_observation_archived_at),
    (4, _migration_4_capture_purged_at),
    (5, _migration_5_rfi_survey_1420_type),
    (6, _migration_6_calibration),
    (7, _migration_7_calibration_sweep_type),
    (8, _migration_8_driftscan),
    (9, _migration_9_schedule),
    (10, _migration_10_station_uuid),
]


def migrate(engine: Engine) -> None:
    """Walk the database forward to the latest schema version.

    Runs, in order, every migration in :data:`MIGRATIONS` whose version is
    above the database's current ``PRAGMA user_version``; each migration and
    its version bump run inside one transaction, so a failed migration leaves
    the database at the previous version. Already-current databases are a
    no-op.

    Parameters
    ----------
    engine : Engine
        Engine from :func:`get_engine`.

    Raises
    ------
    ValueError
        If :data:`MIGRATIONS` versions are not strictly increasing.
    """
    versions = [version for version, _ in MIGRATIONS]
    if versions != sorted(set(versions)):
        raise ValueError(f"MIGRATIONS versions must be strictly increasing, got {versions}")
    for version, apply in MIGRATIONS:
        with engine.begin() as conn:
            current = int(conn.exec_driver_sql("PRAGMA user_version").scalar_one())
            if version <= current:
                continue
            apply(conn)
            conn.exec_driver_sql(f"PRAGMA user_version = {version:d}")


def init_db(data_dir: str | Path) -> Engine:
    """Open (creating and migrating as needed) the station database.

    This is what the server lifespan calls on start: :func:`get_engine`
    followed by :func:`migrate`.

    Parameters
    ----------
    data_dir : str or Path
        The station data directory (``Settings.data_dir``).

    Returns
    -------
    Engine
        A ready-to-use engine at the latest schema version.
    """
    engine = get_engine(data_dir)
    migrate(engine)
    return engine


def session(engine: Engine) -> Session:
    """Open a new :class:`~sqlmodel.Session` (usable as a context manager).

    Parameters
    ----------
    engine : Engine
        Engine from :func:`get_engine` / :func:`init_db`.

    Returns
    -------
    Session
        A new session; the caller closes it (``with session(engine) as s:``).
    """
    return Session(engine)
