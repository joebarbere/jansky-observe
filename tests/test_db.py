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
    engine = db.init_db(tmp_path / "data")  # existing station at the current latest version
    assert _user_version(engine) == 2

    calls: list[int] = []

    def _migration_3_station_backlash(conn: Connection) -> None:
        calls.append(3)
        conn.exec_driver_sql(
            "ALTER TABLE station ADD COLUMN backlash_az_deg FLOAT NOT NULL DEFAULT 0.0"
        )

    monkeypatch.setattr(db, "MIGRATIONS", [*db.MIGRATIONS, (3, _migration_3_station_backlash)])
    db.migrate(engine)
    assert calls == [3]
    assert _user_version(engine) == 3
    columns = {c["name"] for c in inspect(engine).get_columns("station")}
    assert "backlash_az_deg" in columns

    db.migrate(engine)  # already current — must not run again
    assert calls == [3]
    assert _user_version(engine) == 3


def test_fresh_db_ends_at_latest_appended_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []

    def _migration_3(conn: Connection) -> None:
        calls.append(3)
        conn.exec_driver_sql("ALTER TABLE station ADD COLUMN test_col FLOAT DEFAULT 0.0")

    monkeypatch.setattr(db, "MIGRATIONS", [*db.MIGRATIONS, (3, _migration_3)])
    engine = db.init_db(tmp_path / "data")
    assert calls == [3]
    assert _user_version(engine) == 3


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
    assert _user_version(engine) == 2
    assert "stellarium_url" in _station_columns(engine)
    with db.session(engine) as s:
        assert s.exec(select(Station)).one().stellarium_url is None


@pytest.mark.parametrize("truly_old", [True, False])
def test_migration_2_upgrades_version_1_db_keeping_data(tmp_path: Path, truly_old: bool) -> None:
    engine = _db_at_version_1(tmp_path, truly_old)
    assert _user_version(engine) == 1
    assert ("stellarium_url" in _station_columns(engine)) is not truly_old

    db.migrate(engine)
    assert _user_version(engine) == 2
    assert "stellarium_url" in _station_columns(engine)
    with db.session(engine) as s:
        station = s.exec(select(Station)).one()
        assert station.notes == "pre-upgrade data"  # existing row survived
        assert station.stellarium_url is None

    db.migrate(engine)  # re-run: a no-op
    assert _user_version(engine) == 2
    with db.session(engine) as s:
        assert s.exec(select(Station)).one().notes == "pre-upgrade data"
