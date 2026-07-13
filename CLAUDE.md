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
  - `astro/` — astropy pointing + spectral axes: az/el, transit, drift rate, beam-crossing
    (`pointing.py`); topocentric ↔ v_LSR conversion and the Doppler window (`lsr.py`)
  - `confirm/` — the deterministic spectrum classifiers (plan §6): v1 `hline_v1`
    (baseline fit → peak search in the LSR Doppler window → SNR verdict 5/2 thresholds);
    v2 `hi4pi_xcheck` is deferred to jansky-research plan 78 (harness built once there)
  - `weather/` — `WeatherProvider` protocol: NWS primary, Open-Meteo fallback
  - `export/` — one-way exporters for the amateur-HI ecosystem (plan §4.7): Virgo-style CSV
    and ezRA `.txt` from a capture's averaged spectrum; internal formats stay SigMF + `.npz`
  - `photos.py` — photo ingest/resize + the highlight-photo rules (one highlight per
    observation; it leads the PDF report)
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
| `v1.0.0` | — | After one real end-to-end observing campaign, from whatever `v0.x` is current |

M6–M9 (v0.7.0–v0.9.0 and v1.1.0) are specified in `plans/roadmap-post-v0.6.md`; none of
them move the `v1.0.0` gate.

The `/release` skill encodes the whole procedure — use it, don't tag by hand. If
`deploy/install.sh` or `deploy/OS_IMAGE` changed since the last tag, `make qemu-install` MUST
pass before tagging (release-blocking, plan §9). A tag whose install gate fails publishes
nothing by design.

## Current status

M0–M8 have all shipped — **`v0.9.0` (M8 "Research bridge & guides") is released** (tagged
`v0.9.1` after a format-check re-tag; `v0.9.0` is inert), on top of the M7 calibration/scheduling
milestone, the M6 cockpit, and the `v0.6.x` feature-complete base: capture (synthetic + real
Airspy), observation records + wizard + observing ladder, the hline_v1 classifier + live badge +
LSR axes, PDF reports + photos + Virgo/ezRA exporters, Stellarium view-slew + cross-check, HackRF
RFI sweep, gpsd locations, the cockpit (diagnostics + status bar + archive/purge + dark mode +
localization + spectrum audio + `--fps` knob + RFI-survey template), M7 (calibration
epochs + kinds, an offline sky chart, drift-scan campaigns with sidereal-day tagging, and the
unattended transit scheduler + session timer), and M8 (a stable station UUID, the codified
JSON+npz observation bundle, and printable build + observation guide PDFs) over 20 MCP tools.
Schema is at `user_version` 10. **v1.0.0 is not a feature: it is tagged after one real
end-to-end observing campaign (plan → observe → confirm → PDF), from whatever `v0.x` is
current** — prerequisites are physical (feed chain connected, Sun pointing calibration run).
**M9 (v1.1.0, "Rotator — Discovery Drive") is the next milestone**
(`plans/roadmap-post-v0.6.md`). The one remaining M8 follow-up is cross-repo: the
**jansky-research pull skill** (lives in `../jansky-research`, consumes the bundle over MCP) —
jansky-observe's side of that contract (the bundle format + the tools it calls) already ships.
The HI4PI cross-check (`hi4pi_xcheck`, v2) remains **deferred to jansky-research plan 78** per
plan §6 — the comparison harness is built once there and consumed here. The full plan lives in
`plans/jansky_observe.md` — read it before any feature work.

**M9 (v0.10.0 "Rotator — Discovery Drive") is in progress** on `main` — synthetic-first
(built against a simulated rotator; the Drive is still on the wishlist). Decisions (2026-07-13):
**both transports** (rotctl-TCP + EasyComm II, adds `pyserial`) and a **guarded `slew_rotator`
MCP verb** — the first mutating MCP verb that moves hardware, gated on the station az/el limits +
timeline logging (the bias-tee invariant is untouched). It ships as **v0.10.0** (no campaign has
tagged v1.0.0 yet), shifting the table.
- **Rotator client + config + simulator** (piece 1, schema `user_version` **11**,
  `_migration_11_station_rotator`): `astro/rotator.py` — the `Rotator` transport protocol
  (`get_position`/`set_position`/`stop`/`close`) with `RotctlTcpRotator` (stdlib socket, rotctld
  `P`/`p`/`S`), `EasyCommSerialRotator` (EasyComm II over an injectable serial transport; pyserial
  at runtime), and `SimRotator` (finite slew rate under an injectable clock). `within_limits` +
  `make_rotator` are model-free; `server/rotator.py` is the Station glue (`rotator_from_station`,
  `station_allows` limit check, `park_position`). Station gains `rotator_kind`
  (none·sim·rotctl·easycomm) + host/port/serial/baud + az/el limits + park az/el (migration 11).
  Remaining M9 pieces: (2) slew/readback/stop/park UI (HTML, limit-checked, logged), (3) tracking
  mode + scheduler auto-slew, (4) `get_rotator_status` + the guarded `slew_rotator` MCP verb +
  the USB-serial udev rule.

**M8 (v0.9.0 "Research bridge & guides") shipped** — released as `v0.9.1` (see
`plans/roadmap-post-v0.6.md` and `CHANGES.md`). Its pieces:
- **Station UUID** (schema `user_version` **10**, `_migration_10_station_uuid`):
  `Station.uuid` — a stable UUID4 (`models.new_station_uuid`) generated once at seed and
  backfilled onto existing stations by migration 10 (the column is indexed, so the migration
  adds it under a `PRAGMA table_info` guard, backfills a generated UUID per row, and re-creates
  `ix_station_uuid`). It is the station's permanent *machine* identity, distinct from the
  editable `name`. Surfaced by `GET /api/station` + the `get_station_identity` MCP tool,
  shown on the `/station` page, and stamped into the PDF report footer. It is
  jansky-research plan 78's per-station key and the anchor the codified observation bundle carries.
- **Codified observation bundle** (`export/bundle.py`, `BUNDLE_SCHEMA =
  "jansky-observe.observation-bundle/1"`): the documented JSON+npz export plan 78 consumes.
  `build_observation_manifest` produces the machine-readable block (station UUID, pointing, LST
  at each capture's start, timestamps, SDR settings/gain, cal-epoch ref, classifier verdicts);
  `write_observation_bundle` zips it as `bundle.json` + one self-describing averaged-spectrum
  `capture-<id>.npz` per on-disk npz capture. Served at `GET /api/observations/{id}/bundle.json`
  (manifest) and `/bundle` (zip), via the `get_observation_bundle` MCP tool,
  linked from the observation detail page, and **embedded verbatim in the PDF report** so a
  report alone is machine-recoverable. The one-way Virgo/ezRA exporters are deliberately left as
  strict third-party formats — the UUID/provenance rides this bundle, not those.
- **Guide PDFs** (`export/guides.py`, `export/flowsvg.py`, `templates/guide.html`, `routers/
  guides.py`): a **build guide** (authored from the plan's hardware chain) and an **observation
  guide** (generated from the seeded ObservationTypes so it always matches the wizard), both
  through the WeasyPrint pipeline at `GET /guides/{build,observation}.pdf` (index at `/guides`).
  WeasyPrint runs no JS, so mermaid can't render at build time — the per-stage flow diagrams are
  **deterministic inline SVG** (`vertical_flow_svg`) instead. Two house rules for every
  step-type guide PDF, enforced by tests: **every step gets a checkbox**, and a build stage's
  **diagram nodes are a subset of its checkbox parts** (box → part → checkbox). The build guide
  reinforces the **bias-tee-OFF** invariant with a safety callout and an explicit "internal tee
  must never be enabled" step. Remaining M8 work: the **jansky-research pull skill** (cross-repo,
  lives in `../jansky-research`) — jansky-observe's side of that contract (the bundle format +
  the MCP tools it calls) is already done.

**M6 (v0.7.0 "station cockpit") shipped** — all seven pieces below, released as `v0.7.0`
(see `plans/roadmap-post-v0.6.md`).
- **Diagnostics**: `GET /api/diagnostics` + the `get_diagnostics` MCP tool (now **18 tools**),
  a best-effort bundle in `server/diagnostics.py` (systemd units → SDR USB → daemon
  reachability + frame age → Pi thermals → disk → DB schema version → journal errors), each
  check degrading to `"unavailable"` off the Pi. `/troubleshoot-chain` calls it first.
- **Status bar**: `GET /api/status_bar` + `server/status_bar.py` feed a `#cockpit-bar`
  (`templates/_cockpit_bar.html` + `static/statusbar.js`) that rides every page (included in
  both `base.html` and the standalone `index.html`): UTC/local clocks (client-side) + LST
  (astropy, `pointing.local_sidereal_time_hours`, ticked at the sidereal rate between polls),
  station chip (name + Δaz/Δel or "uncalibrated"), source badge (source + fps + stale dot),
  ~15-min-cached weather chip, disk gauge (free + est. SigMF hours; amber<20 %/red<10 %).
- **Archive/soft-delete**: `observation.archived_at` (migration 3) + `capture.purged_at`
  (migration 4). `POST /observations/{id}/archive|unarchive` hides from the default list AND
  `GET /api/observations` (never over MCP; restorable; `?show_archived=1` reveals). `POST
  /captures/{id}/purge` deletes the on-disk file(s) but keeps the row + provenance
  (`purged_at`). All HTML-only — no new MCP verbs.
- **Dark mode + localization**: theme is CSS-variable driven — dark default, light palette
  under `:root[data-theme="light"]` + a `prefers-color-scheme` fallback; `static/ui.js`
  cycles auto→light→dark (persisted, no-flash head script) and `waterfall.js` reads its
  canvas colors from `--wf-*`/`--trace-*` so the plots track the theme. Timestamps: the `dt`
  filter emits `<time class="ts" datetime="…Z">…UTC</time>` and `ui.js` rewrites them to the
  viewer's locale or UTC (toggle persisted); UTC stays canonical in the DB and exports. Both
  toggles live in the cockpit bar.
- **Spectrum audio**: `static/audio.js` (`window.SpectrumAudio`) sonifies the live PSD the
  WebSocket already delivers — client-side WebAudio, no backend. `waterfall.js`'s `onFrame`
  feeds each frame; a live-view control (on/off + mode select) wires it. Four modes:
  receiver (pink-noise filter bank shaped by the band), doppler (peak→pitch, SNR→loudness),
  geiger (click rate ∝ SNR), drone (harmonic pad by band power). Aesthetic only — the
  waterfall stays the quantitative view; guarded on AudioContext.
- **FPS knob**: the capture daemon's `--fps` (default 4.0) is now an operator knob —
  `JANSKY_OBSERVE_FPS` in `/etc/default/jansky-observe`, passed via the capture unit
  (`--fps ${JANSKY_OBSERVE_FPS}`, mirroring `--source`; both the embedded copy in
  `install.sh` and `deploy/systemd/` updated, drift check green). Rationale recorded in the
  env file: each row is an integrated spectrum, so 4 fps is a spectrometer cadence, not a
  render limit. `waterfall.js` adds sub-frame **smooth-scroll interpolation** (rAF loop, EMA
  of the observed frame gap; honors `prefers-reduced-motion`).
- **RFI-survey template**: a seeded `"RFI survey @ 1420"` ObservationType (migration 5, an
  idempotent reseed) with a before/after checklist that drives the existing HackRF
  `rfi_sweep`. `hackrf_sweep.compare_sweeps` + `rfi_sweep_comparison` reduce the first/last
  `hackrf_sweep_csv` captures to the bins that rose ≥ 6 dB; the summary renders on the
  observation detail (`_rfi_comparison.html`) and in the PDF report. Read-only over the CSVs;
  no new MCP verbs.

**M7 (v0.8.0 "Calibration & scheduling") shipped** — all four pieces below, released as
`v0.8.0` (`plans/roadmap-post-v0.6.md`). **M8 (v0.9.0 "Research bridge & guides") is next.**
- **Calibration captures**: `CalibrationEpoch` + `Capture.kind` (science / ref_load / cold_sky /
  hot_ground) + `Capture.cal_epoch_id` (migration 6 = table + columns; migration 7 = reseed for
  the "Calibration sweep" ObservationType). A science capture is stamped at registration with
  the latest epoch (`captures.latest_cal_epoch_id`); calibration captures are marked from the
  observation's capture list (`POST /captures/{id}/kind`, HTML-only) and attach to the current
  epoch. `server/routers/calibration.py`: a `/calibration` page + `POST /calibration/epochs` +
  `GET /api/calibration_epochs`. Cal provenance surfaces in the capture meta (kind +
  cal_epoch_id), the calibration API, and the PDF report (a Calibration section + per-capture
  kind).
- **Sky chart**: a `/sky` canvas alt/az plot (`static/skychart.js`) fed by `GET /api/sky_chart`
  — catalog sources (station offsets applied), Sun, Moon, the galactic plane, and the beam
  cone at a running session's dialed pointing. Positions from `astro/skychart.py` (pure
  astropy, offline); nav link "Sky".
- **Drift-scan campaign mode**: `Campaign` (name, source, fixed az/el, status) + `Capture.
  campaign_id` + `Capture.sidereal_day` (migration 8). Captures registered while a campaign is
  active are auto-tagged (`captures.active_campaign` + `pointing.sidereal_day_number` — a
  monotonic counter that increments once per sidereal day, longitude-rolled). `routers/
  campaigns.py`: a `/campaigns` page + `/campaigns/{id}` detail grouping captures into passes
  by sidereal day (each with its LST for stacking) + `POST /campaigns` / `.../status` / `POST
  /captures/{id}/campaign` (HTML-only) + `GET /api/campaigns[/{id}]`. Nav link "Campaigns".
- **Scheduler + session timer**: a `Schedule` (source, lead_min, run_min, format, repeat
  once|daily, enabled — migration 9). `server/scheduler.py` runs a lifespan loop
  (`scheduler_loop` → `scheduler_tick` every 20 s) that, per enabled schedule, computes the
  firing window `[transit − lead, +run]` (astropy) and drives the daemon over the control
  channel: `next_decision` (pure, tested) starts a capture when a window is open **and nothing
  is capturing** (the daemon stays the only SDR owner), stops it at the window end (registers
  it, disables a `once`), and the disk-red **guardrail** (`would_exceed_disk_red`) refuses a
  run that would fill the disk. `routers/schedules.py`: `/schedules` CRUD page + `GET
  /api/schedules` (with the next window) + `GET /api/scheduler_status`. Nav link "Schedules".
  Session timer: a client-side elapsed readout (`ui.js`, `.session-timer[data-since]`) on a
  running observation. HTML-only; no new MCP verbs.

## Skills & agents

- `/verify` — the pre-commit gate: lint → typecheck → coverage → the end-to-end synthetic smoke.
- `/release` — the milestone-close procedure (QEMU gate check, tag, watch the install gate,
  upgrade the real Pi).
- `/synthetic-fixture` — generate deterministic synthetic IQ/spectrum fixtures so DSP and
  classifier tests never need hardware or sky.
- `/plan-session` — "what should I point at tonight?": whats-up + weather via the station
  MCP, ranked by the observing ladder, ending in a pre-filled draft Observation.
- `/troubleshoot-chain` — the no-signal decision tree in strict order; **step 0 is the
  `get_diagnostics` bundle** (pre-answers every software step), then injector current →
  bias-tee states *checked never changed* → gain → USB → daemon → frequency → tinySA.
- `/new-migration` — scaffold a forward migration: the next `(N, callable)` in
  `db.py`'s `MIGRATIONS`, the matching `models.py` change, and the round-trip test.
- `/observing-copilot` — the during-session companion: live status + the HI badge (SNR
  trend), drift warnings (nudge at ~10° ≈ half the 21° beam, minutes-to-nudge from the
  drift rate), RFI-vs-line judgment, timestamped notes on request.
- `/analyze-observation` — post-session: run `hline_v1` over every `.npz` capture, fetch
  v_LSR spectra, interpret (baseline, SNR, velocity plausibility, RFI), write an analysis
  note whose claims cite `ClassifierResult` rows (provenance rule, plan §12.5).
- `/write-up` — draft the report narrative (plan §7 sections: attempted / conditions /
  recorded / classifier found / interpretation / next steps) in house honesty standards,
  append it to the observation notes, then `build_report` for the PDF. No unsupported
  detection language; an implausible-velocity `detected` is written up as probable RFI.
- `/compare-observations` — cross-session, same source: stacking, SNR vs integration time
  (√t law), peak-v_LSR stability (secular drift = frequency-cal alarm), pointing
  repeatability (growing offsets → redo Sun cal); comparison table appended to the newest
  observation.
- `hi-data-analyst` (agent) — the analyst persona: station numbers, 21 cm physics, the v1
  classifier's exact semantics, and the wrong-spectrum suspect order (RFI → baseline ripple →
  frequency cal → pointing) baked in.
- `dsp-reviewer` (agent) — read-only review of diffs touching `capture/dsp.py`, `astro/`, or
  `confirm/`: plan-number checks plus the classic unit bugs (topocentric vs LSR, Hz vs km/s,
  10 vs 20·log10, fftshift, int16 scaling, Welch normalization, Doppler conventions).

With M4's `/write-up` and `/compare-observations`, every asset in plan §12.6 has shipped.
