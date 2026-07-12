---
name: new-migration
description: Scaffold a forward schema migration — the next (N, callable) appended to db.py's MIGRATIONS (PRAGMA user_version), the matching models.py change, and the round-trip test. Use for ANY schema change (new column, table, or seed row); schema never changes any other way.
---

# New migration: the schema only moves forward

The database migrates forward on server start (plan §9): `src/jansky_observe/db.py` keeps an
ordered `MIGRATIONS` list of `(version, apply)` pairs tracked in SQLite's
`PRAGMA user_version`. Migration 1 builds the full current schema via `create_all` + seeds,
so every later migration is a plain DDL callable appended to the list — and **both paths must
agree**: a fresh database (migration 1, latest models) and a walked-forward old database must
land on the identical schema.

## 1. Read the current state

Read `src/jansky_observe/db.py` — the `MIGRATIONS` list and the worked example in its module
docstring (`_migration_2_station_backlash`). The new migration is version
`N = max(existing) + 1`.

## 2. Write the migration

Append `(N, callable)` to `MIGRATIONS` in `db.py`, following the docstring example:

- Plain `conn.exec_driver_sql(...)` with **ALTER TABLE / INSERT** statements — no SQLModel,
  no `create_all`, no imports of model classes (the migration must keep working even after
  the models change again later).
- New NOT NULL columns need a `DEFAULT` (existing rows must survive).
- New seed rows are `INSERT`s here, *and* in `seeds.py` if fresh databases should get them
  via migration 1.
- One migration per schema change; a short docstring saying what and why.

## 3. Update `models.py` to match

Add the same field/table to the SQLModel class so migration 1's `create_all` builds the
latest schema for fresh databases. Field defaults must match the migration's SQL `DEFAULT`.

## 4. Write the round-trip test

Copy the existing pattern in `tests/test_db.py` (`test_appended_migration_runs_exactly_once`
/ `test_fresh_db_ends_at_latest_appended_version`) — but for a *real* shipped migration,
test the real list, no monkeypatching:

- **Fresh path:** `init_db` on an empty dir lands at `user_version == N` and the new
  column/table/row exists.
- **Upgrade path:** build a database at `N-1` (run `migrate` with `MIGRATIONS[:-1]`), put a
  row of data in, then run the full `migrate` — ends at `N`, the pre-existing data intact,
  and re-running `migrate` is a no-op.

## 5. Verify

Run `/verify` — lint, typecheck, coverage, and the end-to-end smoke (which exercises
`init_db` on server start).

## The rules (non-negotiable)

- **Never edit an existing migration.** Once a version has shipped it has run on real
  databases; a fix is a *new* migration N+1.
- **Never renumber.** Versions are strictly increasing, append-only; `migrate()` rejects
  anything else.
- **Schema changes require a minor version bump** pre-1.0 (minor = milestone/schema, patch =
  fixes) — flag it so the release carries the new `user_version`.
