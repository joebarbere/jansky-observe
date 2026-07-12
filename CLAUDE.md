# jansky-observe — guide for Claude

**What this is.** Observation-management software for the Discovery Dish station: plan, run, and
record attended radio observations on the KrakenRF Discovery Dish 700 mm + H-line feed (1420 MHz)
→ Airspy Mini → Raspberry Pi 5. Two tiers: a Python server (FastAPI + a separate SDR-owning
capture daemon) and a thin browser UI. Sibling of the [`jansky`](https://github.com/joebarbere/jansky)
course (checked out at `../jansky` — we depend on it as a library) and
[`jansky-research`](https://github.com/joebarbere/jansky-research) (`../jansky-research`). A new
session: read both siblings' `CLAUDE.md` too — the conventions here mirror theirs.

## ⚠️ Safety invariants

**The bias-tee rule (hardware-damage risk — non-negotiable).** The Airspy Mini's internal bias
tee supplies 4.5 V at ~50 mA. The H-line feed draws **120 mA** — the internal bias tee can NOT
power it and must stay **OFF, always**. The feed is powered by the dedicated inline USB-C
bias-tee injector instead. Concretely:

- Any diff touching `src/jansky_observe/capture/profiles.py` or device handling MUST preserve
  `bias_tee=False` in the H-line profile (`HLINE_AIRSPY`) AND keep the guard test in
  `tests/test_profiles.py` passing. Never weaken or delete that test.
- Never add `airspy_rx -b 1` or Soapy bias-tee enablement to any H-line code path —
  `src/jansky_observe/capture/airspy_cli.py` (which builds the `airspy_rx` command line) is a
  bias-tee-guarded path just like `profiles.py`.
- The future MCP surface will never expose bias-tee control in any form (plan §12.4) — the
  guardrail is structural, not behavioral. Don't add such a tool, ever.

If a change seems to require enabling the internal bias tee, it's wrong — stop and say so.

## Working rules

- **uv for everything.** `make setup` / `test` / `cov` / `lint` / `fmt` / `typecheck`
  (see `make help`). Never call pip or a bare `python`.
- **85% coverage floor** (`make cov`), `ruff` (line-length 100) + `mypy` clean before every PR.
- **Branch before committing — never commit on `main`.** Open a PR, squash-merge, delete the
  branch.
- Run `/verify` before any commit — it includes the end-to-end synthetic smoke, not just the
  static checks.
- **Schema changes go through `/new-migration` — never edit or renumber an existing
  migration.**

## Layout

- `src/jansky_observe/`
  - `capture/` — the SDR-owning daemon: `daemon.py` (entry `jansky-observe-capture`),
    `sources.py` (`SDRSource` protocol + `SyntheticHISource`), `airspy_cli.py` (the real Airspy
    via `airspy_rx` subprocess — bias-tee-guarded, see above), `writer.py` (capture writers:
    `.npz` + SigMF), `dsp.py` (Welch PSD),
    `profiles.py` (device profiles incl. the bias-tee-guarded `HLINE_AIRSPY`)
  - `server/` — FastAPI app: `app.py` (`create_app` / `app`), `cli.py` (entry `jansky-observe`),
    `routers/` (REST CRUD + the session wizard), `templates/`, `static/waterfall.js` (the
    canvas live view)
  - `mcp/` — the MCP tool surface mounted at `/mcp` on the API server (read + safe verbs
    only; no bias-tee control, no deletes — plan §12.4)
  - `models.py` — the SQLModel data model (Station, Observation, Capture, checklists, … —
    plan §3)
  - `db.py` — SQLite engine + forward-migration-on-start (`MIGRATIONS` on
    `PRAGMA user_version`; change schema only via `/new-migration`)
  - `seeds.py` — seed data: source list + the observing-ladder ObservationTypes (plan §5.4)
  - `astro/` — astropy pointing: az/el, transit, drift rate, beam-crossing (`pointing.py`)
  - `weather/` — `WeatherProvider` protocol: NWS primary, Open-Meteo fallback
  - `frames.py` — spectral-frame wire formats shared by daemon and server (ZMQ + WebSocket)
  - `control.py` — the ZMQ REP control channel (capture start/stop, daemon ↔ server)
  - `synthetic.py` — synthetic noise + fake-HI generators (M0 skeleton, test fixtures)
  - `config.py` — `JANSKY_OBSERVE_*` env settings
- `tests/` — pytest, synthetic fixtures only, no hardware (incl. `test_profiles.py`, the
  bias-tee guard)
- `deploy/` — `install.sh`, `OS_IMAGE` (pinned Pi OS image), `systemd/`, `udev/`,
  `qemu/run-install-test.sh` (`make qemu-install`)
- `plans/jansky_observe.md` — the full project plan
- `.github/workflows/` — `ci.yml`, `release.yml` (release-blocking install gate)

## Releases

Pre-1.0 semver: **minor = milestone, patch = fixes between milestones.**

| Tag | Milestone | Release means |
|---|---|---|
| `v0.1.0` | M0 | Walking skeleton + the whole pipeline (CI, release workflow, `install.sh`, QEMU gate) |
| `v0.2.0` | M1 | First light: real Airspy, live view, captures to disk |
| `v0.3.0` | M2 | Observation records, checklists, session wizard |
| `v0.4.0` | M3 | Confirmation: v1 classifier + HI4PI cross-check |
| `v0.5.0` | M4 | Reports & photos: PDF export, exporters |
| `v0.6.0` | M5 | Feature-complete — the `v1.0.0` release candidate |
| `v1.0.0` | — | After one real end-to-end observing campaign on a `v0.6.x` install |

The `/release` skill encodes the whole procedure — use it, don't tag by hand. If
`deploy/install.sh` or `deploy/OS_IMAGE` changed since the last tag, `make qemu-install` MUST
pass before tagging (release-blocking, plan §9). A tag whose install gate fails publishes
nothing by design.

## Current status

M0 (walking skeleton + pipeline) and M1 (first light: real Airspy, capture writers, control
channel) have shipped — `v0.2.0`. **M2 (observation records) is in progress on
`m2/observation-records`:** the SQLModel data model + forward migrations (`models.py`,
`db.py`, `seeds.py`), astropy pointing (`astro/`) + weather (`weather/`), CRUD + the session
wizard, and the MCP surface mounted at `/mcp` on the API server. **M3 (confirmation) is
next.** The full plan lives in `plans/jansky_observe.md` — read it before any feature work.

## Skills & agents

- `/verify` — the pre-commit gate: lint → typecheck → coverage → the end-to-end synthetic smoke.
- `/release` — the milestone-close procedure (QEMU gate check, tag, watch the install gate,
  upgrade the real Pi).
- `/synthetic-fixture` — generate deterministic synthetic IQ/spectrum fixtures so DSP and
  classifier tests never need hardware or sky.
- `/plan-session` — "what should I point at tonight?": whats-up + weather via the station
  MCP, ranked by the observing ladder, ending in a pre-filled draft Observation.
- `/troubleshoot-chain` — the no-signal decision tree in strict order (injector current →
  bias-tee states *checked never changed* → gain → USB → daemon → frequency → tinySA).
- `/new-migration` — scaffold a forward migration: the next `(N, callable)` in
  `db.py`'s `MIGRATIONS`, the matching `models.py` change, and the round-trip test.

More arrive with the milestones per plan §12.6: `/observing-copilot`, `/analyze-observation`,
`hi-data-analyst`, `dsp-reviewer` at M3; `/write-up`, `/compare-observations` at M4.
