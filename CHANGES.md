# Changelog

All notable changes to jansky-observe, newest first. Versions map to milestones from
`plans/jansky_observe.md` (pre-1.0 semver: **minor = milestone, patch = fixes between
milestones**). Work that landed outside a milestone gets a brief summary under the release
that shipped it. Maintained as part of `/release` — a release isn't finished until its
section exists here.

## v0.8.0 — 2026-07-12 — M7 "Calibration & scheduling"

The milestone the jansky-research station track (plans 78/79/80/84) was waiting for. Four
pieces; schema advances to `user_version` 9. No `install.sh` change (migrations run on start).

- **Calibration captures** (#25): a `CalibrationEpoch` object + `Capture.kind` (science /
  ref_load / cold_sky / hot_ground) + `Capture.cal_epoch_id`. Every science capture is stamped
  at registration with the calibration epoch in effect; calibration captures are marked from
  the observation's capture list. A `/calibration` page, a guided "Calibration sweep"
  ObservationType, and cal-epoch provenance in the capture meta + the PDF report. Migrations 6, 7.
- **Sky chart** (#26): a `/sky` offline alt/az canvas — catalog sources (station offsets
  applied), Sun, Moon, the galactic plane, and the beam cone at a running session's pointing —
  all from astropy (`GET /api/sky_chart`). The always-available answer to desktop Stellarium.
- **Drift-scan campaign mode** (#27): a `Campaign` (fixed-pointing over many nights) +
  `Capture.campaign_id` + `Capture.sidereal_day`. Captures taken while a campaign is active are
  tagged with a sidereal-day number so passes at the same LST stack; a `/campaigns` detail
  groups captures into passes by sidereal day with their LST. Migration 8.
- **Scheduler + session timer** (#28): a `Schedule` (source, lead/run minutes, format, once |
  daily). A server loop starts a capture `lead` minutes before the source's transit and runs it
  `run` minutes, driving the daemon over the control channel (the daemon stays the only SDR
  owner; never overlaps a running capture; refuses a run that would fill the disk past red).
  A `/schedules` page; a client-side session timer on running observations. Migration 9.

## v0.7.1 — 2026-07-12

Maintenance release, no milestone — fixes the theme regression reported after v0.7.0.

- **Fix a broken CSS comment that could unstyle the whole UI.** A stray `*/`
  inside a header comment (`--wf-*/--trace-*`) closed the comment early and made
  the parser swallow the base `:root` dark palette. It was masked in v0.7.0 by
  the `prefers-color-scheme` fallback (which still supplied vars on a light-OS
  machine), so a light-preferring laptop rendered a light UI and a dark-OS one
  could render unstyled. Comment reworded; a test now asserts balanced `/* */`
  and that the dark `:root` survives.
- **Dark is now the default regardless of the OS preference** (the app is built
  around a waterfall — best on black). The `prefers-color-scheme: light`
  auto-switch is gone; light is opt-in via the cockpit-bar toggle, which is now
  a simple **Dark ↔ Light** switch (was a confusing 3-state Auto→Light→Dark
  where "Auto" and "Light" looked identical on a light laptop). Colored status
  badges keep legible dark text in the light theme.
- **`sqlite3` added to the install dependencies** so the DB under
  `/var/lib/jansky-observe/` can be hand-inspected on the Pi (Pi OS Lite ships
  no CLI; the app itself uses Python's `sqlite3`).

## v0.7.0 — 2026-07-12 — M6 "Station cockpit"

Everything a glance at the UI should answer, plus operator-comfort. Seven pieces; none
move the `v1.0.0` gate (`plans/roadmap-post-v0.6.md`).

- **Diagnostics bundle** (#13): `GET /api/diagnostics` + the `get_diagnostics` MCP tool
  (now 18 tools) — a best-effort debug bundle (systemd units → SDR USB enumeration →
  capture-daemon reachability + last-frame age → Pi thermals → disk → DB schema version →
  journal errors), each check degrading to `"unavailable"` off the Pi. `/troubleshoot-chain`
  calls it first.
- **Status bar** (#15): a `#cockpit-bar` on every page — UTC/local clocks + LST (astropy),
  the active-station chip (Δaz/Δel or "uncalibrated"), the source badge (source + fps + a
  stale dot), a ~15-min-cached weather chip, and the disk gauge (free + estimated SigMF
  hours, amber < 20 % / red < 10 %). `GET /api/status_bar`.
- **Archive / soft-delete** (#17): `POST /observations/{id}/archive|unarchive` hides an
  observation from the default list *and* the MCP surface (restorable; `?show_archived=1`
  reveals). `POST /captures/{id}/purge` reclaims a capture's on-disk file(s) while keeping
  the row + provenance. All HTML-only — no new MCP verbs. Migrations 3 (`observation.
  archived_at`) + 4 (`capture.purged_at`).
- **Dark mode + localization** (#18): a CSS-variable theme (dark default, light palette,
  `prefers-color-scheme` fallback) with an auto→light→dark toggle; the waterfall canvas
  tracks the theme. Timestamps render in the viewer's locale or UTC (toggle); UTC stays
  canonical in the DB and exports.
- **Spectrum audio** (#19): client-side WebAudio sonification of the live PSD — four modes
  (receiver / doppler / geiger / drone). Aesthetic only; guarded on `AudioContext`.
- **FPS knob + smooth-scroll** (#20): `--fps` is now settable from `/etc/default/jansky-
  observe` (`JANSKY_OBSERVE_FPS`); the waterfall interpolates a sub-frame smooth scroll
  (honors `prefers-reduced-motion`).
- **RFI-survey template** (#21): a seeded "RFI survey @ 1420" ObservationType (migration 5)
  with a before/after checklist; `compare_sweeps` summarizes which bins rose, on the
  observation detail and in the PDF report.

Also since v0.6.1: GitHub Sponsors + Ko-fi funding links and badges (#11, #12), the
roadmap-order/wishlist corrections and `install.sh --reset-data` (#12), and removal of the
internal funding-plan doc (#16). Schema is at `user_version` 5.

## v0.6.1 — 2026-07-12

Maintenance release, no milestone.

- Bump the `jansky` library dependency v0.1.0 → v0.2.0 (additive release: new `catalog`,
  `interferometry`, `observing`, `optics` modules; nothing jansky-observe consumes changed).
  Pin updated in CI, the release install gate, and `deploy/install.sh --jansky-ref`.
- Add this changelog.

## v0.6.0 — 2026-07-12 — M5 "Polish" (feature-complete)

The v1.0.0 release candidate: every plan §12.6 deliverable is shipped. Commit: `001ed31` (#8),
docs flip `ba91ec7` (#9).

- Stellarium RemoteControl client + wizard pointing cross-check (view API takes radians,
  azimuth from South; astropy stays authoritative).
- `Station.stellarium_url` via schema migration 2 (first guarded `ALTER TABLE` migration).
- HackRF RFI sweep as a daemon command + `start_rfi_sweep` MCP tool (17 tools total); the
  `-p` antenna-power flag is structurally unemittable, same treatment as the Airspy bias tee.
- gpsd TPV client + "Use GPS fix" for catalog locations.

## v0.5.0 — 2026-07-12 — M4 "Reports & photos"

Commit: `596facc` (#6), docs flip + process fix `289eff7` (#7, made the docs flip a
`/release` step).

- WeasyPrint PDF observation reports (`POST /api/observations/{id}/report`).
- Photo ingest via Pillow (highlight/supporting roles, originals discarded,
  exactly-one-highlight invariant; photo delete is HTML-only, never MCP).
- Virgo two-column CSV and ezRA `.txt` exporters (formats researched from each tool's
  source; ezCon parse-acceptance tested).
- MCP grows `build_report` + `export_capture` (16 tools); skills `/write-up`,
  `/compare-observations`.

## v0.4.0 — 2026-07-12 — M3 "Confirmation"

Commit: `6a4d900` (#5).

- `hline_v1` classifier (plan §6): cubic baseline excluding the Doppler window, SNR
  thresholds detected ≥ 5 / uncertain 2–5.
- `astro/lsr.py` LSR velocity axes (astropy SpectralCoord, lsrk) + per-observation Doppler
  windows.
- Live HI badge on a server-side accumulating average; captures auto-registered as DB rows
  on stop; classify endpoint storing `ClassifierResult` rows + verdict plots.
- MCP grows to 14 tools; skills `/observing-copilot`, `/analyze-observation`; agents
  `hi-data-analyst`, `dsp-reviewer`.
- HI4PI cross-check (v2 classifier) deliberately deferred to jansky-research plan 78.

## v0.3.0 — 2026-07-12 — M2 "Observation records"

Commit: `acaedbd` (#4). Also shipped `94b73d4` (landed on main between milestones): the
observing ladder seeded as ObservationTypes, including the 10-item Sun-calibration checklist.

- SQLModel data model (12 tables) + `PRAGMA user_version` forward migrations.
- Session wizard with required-checklist gating; observation detail with notes timeline.
- `astro/pointing.py` (offsets from the Station record, transit/rise/set, beam crossing);
  NWS → Open-Meteo weather fallback.
- MCP server mounted at `/mcp` (9 tools, read + safe verbs only); skills `/plan-session`,
  `/troubleshoot-chain`, `/new-migration`.

## v0.2.0 — 2026-07-12 — M1 "First light"

Commit: `fa038bb` (#3). First real photons: Airspy Mini streaming on the Pi.

- `AirspyRxSource` (airspy_rx subprocess, drain thread, latest-wins live ring + lossless
  bounded capture tap).
- Capture to `.npz` (averaged spectra) and SigMF (`ci16_le` IQ, 43.2 GB/h at 3 MSPS).
- ZMQ REP control channel (status/start/stop); capture panel + accumulating average in the
  UI; `install.sh --set-source synthetic|airspy`.

## v0.1.1 — 2026-07-12 — M0 "Walking skeleton" (first working release)

Commit: `dff3cca` (#2). Identical to v0.1.0 plus one fix: create `/etc/udev/rules.d` before
writing rules (the v0.1.0 install gate failed in the udev-less release container — the gate
working as designed; v0.1.0 is inert and publishes nothing).

## v0.1.0 — 2026-07-12 — M0 (inert tag)

Commit: `98ec226`. Never published — see v0.1.1.

- Synthetic capture daemon → ZMQ PUB → FastAPI relay → WebSocket → canvas waterfall.
- `deploy/install.sh` (systemd units, udev rules, healthz smoke) + the QEMU install gate
  on genuine Raspberry Pi OS (`dea3460`, #1) + release-blocking install gate in CI.

## Pre-release plan work (unversioned)

Plan-only commits on main before/between releases: `17a0ccc`, `d6e9efb`, `25b690d`
(jansky-research station-track S1–S8 requirements and the Tsys sky/ground pair move into
`plans/jansky_observe.md`).
