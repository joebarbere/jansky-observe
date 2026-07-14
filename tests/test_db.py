"""Engine + forward-migration tests: fresh create, idempotency, appended migrations."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import Connection, Engine, inspect
from sqlmodel import SQLModel, select

from jansky_observe import db
from jansky_observe.models import ObservationType, RadioSource, Station

EXPECTED_TABLES = set(SQLModel.metadata.tables)


def _user_version(engine: Engine) -> int:
    with engine.connect() as conn:
        return int(conn.exec_driver_sql("PRAGMA user_version").scalar_one())


def test_get_engine_creates_data_dir_and_file_path(tmp_path: Path) -> None:
    data_dir = tmp_path / "nested" / "data"
    engine = db.get_engine(data_dir)
    assert data_dir.is_dir()
    assert engine.url.database == str(data_dir / "jansky-observe.sqlite3")


def test_engine_applies_wal_and_pragmas(tmp_path: Path) -> None:
    """Every connection comes up in WAL with the tuned pragmas (flash-storage safety)."""
    engine = db.get_engine(tmp_path / "data")
    with engine.connect() as conn:
        assert conn.exec_driver_sql("PRAGMA journal_mode").scalar_one().lower() == "wal"
        assert int(conn.exec_driver_sql("PRAGMA busy_timeout").scalar_one()) == 5000
        assert int(conn.exec_driver_sql("PRAGMA synchronous").scalar_one()) == 1  # NORMAL
    # A second connection is tuned independently (the listener fires per-connect).
    with engine.connect() as conn2:
        assert conn2.exec_driver_sql("PRAGMA journal_mode").scalar_one().lower() == "wal"


def test_init_db_fresh_creates_everything(tmp_path: Path) -> None:
    engine = db.init_db(tmp_path / "data")
    assert (tmp_path / "data" / "jansky-observe.sqlite3").is_file()
    assert _user_version(engine) == max(version for version, _ in db.MIGRATIONS)
    assert EXPECTED_TABLES <= set(inspect(engine).get_table_names())
    with db.session(engine) as s:
        assert s.exec(select(Station)).one().name == "Discovery Dish"
        assert len(s.exec(select(RadioSource)).all()) == 6


def test_migrate_rerun_is_noop(tmp_path: Path) -> None:
    engine = db.init_db(tmp_path / "data")

    def _counts() -> tuple[int, int, int]:
        with db.session(engine) as s:
            return (
                len(s.exec(select(Station)).all()),
                len(s.exec(select(RadioSource)).all()),
                len(s.exec(select(ObservationType)).all()),
            )

    before = _counts()
    db.migrate(engine)
    db.init_db(tmp_path / "data")  # the server-start path, again
    assert _counts() == before
    assert _user_version(engine) == max(version for version, _ in db.MIGRATIONS)


def test_appended_migration_runs_exactly_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An old database walks forward through a new migration exactly once."""
    latest = max(version for version, _ in db.MIGRATIONS)
    nxt = latest + 1
    engine = db.init_db(tmp_path / "data")  # existing station at the current latest version
    assert _user_version(engine) == latest

    calls: list[int] = []

    def _migration_next_station_backlash(conn: Connection) -> None:
        calls.append(nxt)
        conn.exec_driver_sql(
            "ALTER TABLE station ADD COLUMN backlash_az_deg FLOAT NOT NULL DEFAULT 0.0"
        )

    monkeypatch.setattr(db, "MIGRATIONS", [*db.MIGRATIONS, (nxt, _migration_next_station_backlash)])
    db.migrate(engine)
    assert calls == [nxt]
    assert _user_version(engine) == nxt
    columns = {c["name"] for c in inspect(engine).get_columns("station")}
    assert "backlash_az_deg" in columns

    db.migrate(engine)  # already current — must not run again
    assert calls == [nxt]
    assert _user_version(engine) == nxt


def test_fresh_db_ends_at_latest_appended_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    nxt = max(version for version, _ in db.MIGRATIONS) + 1
    calls: list[int] = []

    def _migration_next(conn: Connection) -> None:
        calls.append(nxt)
        conn.exec_driver_sql("ALTER TABLE station ADD COLUMN test_col FLOAT DEFAULT 0.0")

    monkeypatch.setattr(db, "MIGRATIONS", [*db.MIGRATIONS, (nxt, _migration_next)])
    engine = db.init_db(tmp_path / "data")
    assert calls == [nxt]
    assert _user_version(engine) == nxt


def test_migrate_rejects_non_increasing_versions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "MIGRATIONS", [*db.MIGRATIONS, (1, db.MIGRATIONS[0][1])])
    with pytest.raises(ValueError, match="strictly increasing"):
        db.migrate(db.get_engine(tmp_path / "data"))


# ---- migration 2: station.stellarium_url (the first real shipped migration) --------


def _station_columns(engine: Engine) -> set[str]:
    return {c["name"] for c in inspect(engine).get_columns("station")}


def _db_at_version_1(tmp_path: Path, truly_old: bool) -> Engine:
    """A database stopped at user_version 1, with pre-existing station data.

    ``truly_old=True`` simulates a database created before migration 2 shipped
    (the column is dropped, as create_all built it from the latest models);
    ``False`` is the skill-literal ``MIGRATIONS[:1]`` build, which already has
    the column — migration 2's guard must make both land identically.
    """
    engine = db.get_engine(tmp_path / "data")
    with engine.begin() as conn:
        db.MIGRATIONS[0][1](conn)  # migration 1 only
        if truly_old:
            conn.exec_driver_sql("ALTER TABLE station DROP COLUMN stellarium_url")
        conn.exec_driver_sql("UPDATE station SET notes = 'pre-upgrade data'")
        conn.exec_driver_sql("PRAGMA user_version = 1")
    return engine


def test_migration_2_fresh_db_has_stellarium_url(tmp_path: Path) -> None:
    engine = db.init_db(tmp_path / "data")
    assert _user_version(engine) == max(version for version, _ in db.MIGRATIONS)
    assert "stellarium_url" in _station_columns(engine)
    with db.session(engine) as s:
        assert s.exec(select(Station)).one().stellarium_url is None


@pytest.mark.parametrize("truly_old", [True, False])
def test_migration_2_upgrades_version_1_db_keeping_data(tmp_path: Path, truly_old: bool) -> None:
    engine = _db_at_version_1(tmp_path, truly_old)
    assert _user_version(engine) == 1
    assert ("stellarium_url" in _station_columns(engine)) is not truly_old

    db.migrate(engine)
    assert _user_version(engine) == max(version for version, _ in db.MIGRATIONS)
    assert "stellarium_url" in _station_columns(engine)
    with db.session(engine) as s:
        station = s.exec(select(Station)).one()
        assert station.notes == "pre-upgrade data"  # existing row survived
        assert station.stellarium_url is None

    db.migrate(engine)  # re-run: a no-op
    assert _user_version(engine) == max(version for version, _ in db.MIGRATIONS)
    with db.session(engine) as s:
        assert s.exec(select(Station)).one().notes == "pre-upgrade data"


# ---- migrations 3 & 4: observation.archived_at, capture.purged_at (roadmap M6) ----


def _columns(engine: Engine, table: str) -> set[str]:
    return {c["name"] for c in inspect(engine).get_columns(table)}


def test_migration_3_4_fresh_db_has_new_columns(tmp_path: Path) -> None:
    engine = db.init_db(tmp_path / "data")
    assert _user_version(engine) == max(version for version, _ in db.MIGRATIONS)
    assert "archived_at" in _columns(engine, "observation")
    assert "purged_at" in _columns(engine, "capture")


def test_migration_3_4_upgrade_from_version_2_keeps_data(tmp_path: Path) -> None:
    # Build a database stopped at version 2 (through migration 2), with a row.
    engine = db.get_engine(tmp_path / "data")
    with engine.begin() as conn:
        db.MIGRATIONS[0][1](conn)  # migration 1: full schema + seeds
        db.MIGRATIONS[1][1](conn)  # migration 2: stellarium_url guard
        # Simulate a pre-M6 database: drop the columns create_all built.
        conn.exec_driver_sql("ALTER TABLE observation DROP COLUMN archived_at")
        conn.exec_driver_sql("ALTER TABLE capture DROP COLUMN purged_at")
        conn.exec_driver_sql(
            "INSERT INTO observation (name, observation_type_id, station_id, "
            "location_id, source_id, status, notes, created_at, updated_at) "
            "VALUES ('old obs', 1, 1, 1, 1, 'done', 'keep me', "
            "'2026-07-01T00:00:00', '2026-07-01T00:00:00')"
        )
        conn.exec_driver_sql("PRAGMA user_version = 2")
    assert _user_version(engine) == 2
    assert "archived_at" not in _columns(engine, "observation")

    db.migrate(engine)
    assert _user_version(engine) == max(version for version, _ in db.MIGRATIONS)
    assert "archived_at" in _columns(engine, "observation")
    assert "purged_at" in _columns(engine, "capture")
    with engine.connect() as conn:
        row = conn.exec_driver_sql(
            "SELECT notes, archived_at FROM observation WHERE name = 'old obs'"
        ).one()
    assert row[0] == "keep me"  # pre-existing data survived
    assert row[1] is None  # new column defaults to NULL (active)

    db.migrate(engine)  # re-run is a no-op
    assert _user_version(engine) == max(version for version, _ in db.MIGRATIONS)


# ---- migration 5: the "RFI survey @ 1420" ObservationType (roadmap M6) -------------


def _type_names(engine: Engine) -> set[str]:
    from jansky_observe.models import ObservationType

    with db.session(engine) as s:
        return {t.name for t in s.exec(select(ObservationType)).all()}


def test_migration_5_fresh_db_has_rfi_survey_1420_type(tmp_path: Path) -> None:
    engine = db.init_db(tmp_path / "data")
    assert "RFI survey @ 1420" in _type_names(engine)


def test_migration_6_7_fresh_db_has_calibration(tmp_path: Path) -> None:
    from jansky_observe.models import ObservationType

    engine = db.init_db(tmp_path / "data")
    assert _user_version(engine) == max(version for version, _ in db.MIGRATIONS)
    assert "calibration_epoch" in inspect(engine).get_table_names()
    caps = {c["name"] for c in inspect(engine).get_columns("capture")}
    assert {"kind", "cal_epoch_id"} <= caps
    with db.session(engine) as s:
        names = {t.name for t in s.exec(select(ObservationType)).all()}
    assert "Calibration sweep" in names


def test_migration_6_7_upgrade_from_version_5_keeps_data(tmp_path: Path) -> None:
    from jansky_observe.models import Capture, ObservationType

    # Simulate a pre-M7 database with a pre-existing capture row. Only the
    # FK-free ``kind`` column is dropped: SQLite can't DROP a column bound in a
    # foreign key (``cal_epoch_id``), and migration 6 is guarded/idempotent for
    # the table + FK column anyway (covered by the fresh-DB test + the re-run
    # below). What matters here: existing rows get the default and survive.
    engine = db.get_engine(tmp_path / "data")
    with engine.begin() as conn:
        for version, apply in db.MIGRATIONS[:5]:  # migrations 1..5
            apply(conn)
            conn.exec_driver_sql(f"PRAGMA user_version = {version}")
        conn.exec_driver_sql("ALTER TABLE capture DROP COLUMN kind")
        conn.exec_driver_sql("DELETE FROM observation_type WHERE name = 'Calibration sweep'")
        conn.exec_driver_sql(
            "INSERT INTO capture (device, path, format, size_bytes, created_at) "
            "VALUES ('airspy', '/x/old.npz', 'npz_spectra', 123, '2026-07-01T00:00:00')"
        )
    assert _user_version(engine) == 5
    assert "kind" not in {c["name"] for c in inspect(engine).get_columns("capture")}

    db.migrate(engine)
    assert _user_version(engine) == max(version for version, _ in db.MIGRATIONS)
    assert "calibration_epoch" in inspect(engine).get_table_names()
    with db.session(engine) as s:
        old = s.exec(select(Capture).where(Capture.path == "/x/old.npz")).one()
        assert old.kind == "science"  # existing row got the default
        assert old.cal_epoch_id is None
        assert "Calibration sweep" in {t.name for t in s.exec(select(ObservationType)).all()}

    db.migrate(engine)  # re-run is a no-op
    assert _user_version(engine) == max(version for version, _ in db.MIGRATIONS)


def test_migration_8_fresh_db_has_campaign(tmp_path: Path) -> None:
    engine = db.init_db(tmp_path / "data")
    assert _user_version(engine) == max(version for version, _ in db.MIGRATIONS)
    assert "campaign" in inspect(engine).get_table_names()
    caps = {c["name"] for c in inspect(engine).get_columns("capture")}
    assert {"campaign_id", "sidereal_day"} <= caps


def test_migration_8_upgrade_from_version_7_keeps_data(tmp_path: Path) -> None:
    from jansky_observe.models import Capture

    # Only the FK-free ``sidereal_day`` column is dropped (``campaign_id`` is a
    # foreign key — SQLite won't DROP it); migration 8's table + FK column are
    # guarded/idempotent (fresh-DB test + the re-run below cover them).
    engine = db.get_engine(tmp_path / "data")
    with engine.begin() as conn:
        for version, apply in db.MIGRATIONS[:7]:  # migrations 1..7
            apply(conn)
            conn.exec_driver_sql(f"PRAGMA user_version = {version}")
        conn.exec_driver_sql("ALTER TABLE capture DROP COLUMN sidereal_day")
        conn.exec_driver_sql(
            "INSERT INTO capture (device, path, format, size_bytes, kind, created_at) "
            "VALUES ('airspy', '/x/old.npz', 'npz_spectra', 5, 'science', '2026-07-01T00:00:00')"
        )
    assert _user_version(engine) == 7
    assert "sidereal_day" not in {c["name"] for c in inspect(engine).get_columns("capture")}

    db.migrate(engine)
    assert _user_version(engine) == max(version for version, _ in db.MIGRATIONS)
    assert "campaign" in inspect(engine).get_table_names()
    with db.session(engine) as s:
        old = s.exec(select(Capture).where(Capture.path == "/x/old.npz")).one()
        assert old.sidereal_day is None and old.campaign_id is None  # untagged
    db.migrate(engine)  # re-run is a no-op
    assert _user_version(engine) == max(version for version, _ in db.MIGRATIONS)


def test_migration_9_fresh_db_has_schedule(tmp_path: Path) -> None:
    engine = db.init_db(tmp_path / "data")
    assert _user_version(engine) == max(version for version, _ in db.MIGRATIONS)
    assert "schedule" in inspect(engine).get_table_names()
    cols = {c["name"] for c in inspect(engine).get_columns("schedule")}
    assert {"name", "source_id", "lead_min", "run_min", "format", "repeat", "enabled"} <= cols


def test_migration_9_upgrade_from_version_8_adds_table(tmp_path: Path) -> None:
    engine = db.get_engine(tmp_path / "data")
    with engine.begin() as conn:
        for version, apply in db.MIGRATIONS[:8]:  # migrations 1..8
            apply(conn)
            conn.exec_driver_sql(f"PRAGMA user_version = {version}")
        conn.exec_driver_sql("DROP TABLE IF EXISTS schedule")
    assert _user_version(engine) == 8
    assert "schedule" not in inspect(engine).get_table_names()
    db.migrate(engine)
    assert _user_version(engine) == max(version for version, _ in db.MIGRATIONS)
    assert "schedule" in inspect(engine).get_table_names()
    db.migrate(engine)  # re-run no-op
    assert _user_version(engine) == max(version for version, _ in db.MIGRATIONS)


def test_migration_10_fresh_db_has_station_uuid(tmp_path: Path) -> None:
    from jansky_observe.models import Station

    engine = db.init_db(tmp_path / "data")
    assert _user_version(engine) == max(version for version, _ in db.MIGRATIONS)
    assert "uuid" in {c["name"] for c in inspect(engine).get_columns("station")}
    with db.session(engine) as s:
        station = s.exec(select(Station)).one()
    assert station.uuid and len(station.uuid) == 36  # a real UUID4 string


def test_migration_10_upgrade_from_version_9_backfills_uuid(tmp_path: Path) -> None:
    from jansky_observe.models import Station

    # Simulate a pre-M8 station with no uuid column. The column is indexed, so
    # the index is dropped before the column (SQLite won't DROP an indexed one).
    engine = db.get_engine(tmp_path / "data")
    with engine.begin() as conn:
        for version, apply in db.MIGRATIONS[:9]:  # migrations 1..9
            apply(conn)
            conn.exec_driver_sql(f"PRAGMA user_version = {version}")
        conn.exec_driver_sql("DROP INDEX IF EXISTS ix_station_uuid")
        conn.exec_driver_sql("ALTER TABLE station DROP COLUMN uuid")
    assert _user_version(engine) == 9
    assert "uuid" not in {c["name"] for c in inspect(engine).get_columns("station")}

    db.migrate(engine)
    assert _user_version(engine) == max(version for version, _ in db.MIGRATIONS)
    with db.session(engine) as s:
        first_uuid = s.exec(select(Station)).one().uuid
    assert first_uuid and len(first_uuid) == 36  # existing station got a backfilled uuid

    db.migrate(engine)  # re-run must not regenerate it
    with db.session(engine) as s:
        assert s.exec(select(Station)).one().uuid == first_uuid


def test_migration_11_fresh_db_has_rotator_columns(tmp_path: Path) -> None:
    engine = db.init_db(tmp_path / "data")
    assert _user_version(engine) == max(version for version, _ in db.MIGRATIONS)
    cols = {c["name"] for c in inspect(engine).get_columns("station")}
    assert {
        "rotator_kind",
        "rotator_host",
        "rotator_port",
        "rotator_serial",
        "rotator_baud",
        "az_min_deg",
        "az_max_deg",
        "el_min_deg",
        "el_max_deg",
        "park_az_deg",
        "park_el_deg",
    } <= cols


def test_migration_11_upgrade_from_version_10_keeps_data(tmp_path: Path) -> None:
    from jansky_observe.models import Station

    # Simulate a pre-M9 station: run migrations 1..10, drop the rotator columns
    # (none are indexed or FK-bound, so a plain DROP works), then migrate forward.
    engine = db.get_engine(tmp_path / "data")
    rotator_cols = [
        "rotator_kind",
        "rotator_host",
        "rotator_port",
        "rotator_serial",
        "rotator_baud",
        "az_min_deg",
        "az_max_deg",
        "el_min_deg",
        "el_max_deg",
        "park_az_deg",
        "park_el_deg",
    ]
    with engine.begin() as conn:
        for version, apply in db.MIGRATIONS[:10]:  # migrations 1..10
            apply(conn)
            conn.exec_driver_sql(f"PRAGMA user_version = {version}")
        for name in rotator_cols:
            conn.exec_driver_sql(f"ALTER TABLE station DROP COLUMN {name}")
    assert _user_version(engine) == 10
    assert "rotator_kind" not in {c["name"] for c in inspect(engine).get_columns("station")}

    db.migrate(engine)
    assert _user_version(engine) == max(version for version, _ in db.MIGRATIONS)
    with db.session(engine) as s:
        station = s.exec(select(Station)).one()
        assert station.rotator_kind == "none"  # existing station defaults to manual
        assert station.az_max_deg == 360.0 and station.el_max_deg == 90.0
        assert station.park_el_deg == 90.0

    db.migrate(engine)  # re-run is a no-op
    assert _user_version(engine) == max(version for version, _ in db.MIGRATIONS)


def test_migration_5_seeds_the_type_into_a_version_4_db(tmp_path: Path) -> None:
    from jansky_observe.models import ChecklistTemplateItem, ObservationType

    # Build a database stopped at version 4 (all migrations up to purged_at) but
    # WITHOUT the new type — simulate a pre-M6-final station by deleting it.
    engine = db.get_engine(tmp_path / "data")
    with engine.begin() as conn:
        for version, apply in db.MIGRATIONS[:4]:  # migrations 1..4
            apply(conn)
            conn.exec_driver_sql(f"PRAGMA user_version = {version}")
        conn.exec_driver_sql("DELETE FROM observation_type WHERE name = 'RFI survey @ 1420'")
    assert _user_version(engine) == 4
    assert "RFI survey @ 1420" not in _type_names(engine)

    db.migrate(engine)  # runs migration 5
    assert _user_version(engine) == max(version for version, _ in db.MIGRATIONS)
    assert "RFI survey @ 1420" in _type_names(engine)
    # The before/after checklist rode in with it.
    with db.session(engine) as s:
        rfi = s.exec(
            select(ObservationType).where(ObservationType.name == "RFI survey @ 1420")
        ).one()
        items = s.exec(
            select(ChecklistTemplateItem).where(ChecklistTemplateItem.observation_type_id == rfi.id)
        ).all()
    assert any("BEFORE sweep" in i.text for i in items)
    assert any("AFTER sweep" in i.text for i in items)

    db.migrate(engine)  # re-run is a no-op (idempotent seed)
    assert _user_version(engine) == max(version for version, _ in db.MIGRATIONS)
