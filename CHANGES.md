# Changelog

All notable changes to jansky-observe, newest first. Versions map to milestones from
`plans/jansky_observe.md` (pre-1.0 semver: **minor = milestone, patch = fixes between
milestones**). Work that landed outside a milestone gets a brief summary under the release
that shipped it. Maintained as part of `/release` — a release isn't finished until its
section exists here.

## v0.11.1 — 2026-07-15 — M10 "Sky/ground Tsys" (part 2 of 2 — completes M10)

Second half of milestone M10 (`plans/m10-onoff-and-skyground.md`): the sky/ground **Y-factor
reduction** now lives in the app, turning the runbook's "log this ΔdB weekly" into a first-class,
trendable number. Schema advances to `user_version` **13**. No `install.sh`/`OS_IMAGE` change (no
QEMU gate); the SDR/capture path and bias-tee invariant are untouched.

- **ΔdB + Tsys on the calibration epoch** (schema `user_version` **13**,
  `_migration_13_calepoch_tsys`): `CalibrationEpoch.sky_ground_delta_db` + `tsys_k` (both nullable,
  `None` until computed). A new `confirm/skyground.py::sky_ground_delta(cold_db, hot_db)` computes
  the Y-factor from an epoch's existing `cold_sky` + `hot_ground` captures (the M7 kinds): band-mean
  `y = mean(hot_lin)/mean(cold_lin)`, `delta_db = 10·log10(y)`, `tsys_k = (T_hot − y·T_cold)/(y − 1)`
  with assumed `T_hot = 300 K` / `T_cold = 10 K`; an unphysical `y ≤ 1` (captures swapped) is
  rejected. `POST /calibration/epochs/{id}/tsys` runs and stores it; the `/calibration` page shows a
  per-epoch ΔdB/Tsys column + a "Compute Tsys / sky-ground ΔdB" button, and the PDF report's
  Calibration section renders the values.
- **MCP**: a read-only `get_calibration_epochs` tool returns the per-epoch Tsys/ΔdB series (for
  trending via `/compare-observations` and the analyst agent) — MCP surface **22 → 23 tools**, still
  read-only (the `slew_rotator` exception stands alone).

## v0.11.0 — 2026-07-14 — M10 "Position switching (ON/OFF)" (part 1 of 2)

First half of milestone M10 (`plans/m10-onoff-and-skyground.md`): the app gains genuine
**ON/OFF position switching** — recording an OFF (blank-sky) reference alongside an ON pointing
and confirming HI from their difference — alongside the existing single-capture baseline-fit
classifier. Schema advances to `user_version` **12**. No `install.sh`/`OS_IMAGE` change (so no
QEMU gate). Read-and-reduce only: the SDR/capture path and the bias-tee invariant are untouched,
and no new mutating MCP verb (MCP stays 22 tools). The sky/ground Tsys reduction (M10 Piece 3)
follows in `v0.11.1`.

- **Capture position + pairing** (schema `user_version` **12**, `_migration_12_capture_position`):
  `Capture.position` (`"on"` default / `"off"`) — *orthogonal* to the M7 calibration `kind`
  (an ON or OFF pointing is still a `science` capture) — plus `Capture.pair_capture_id`, the ON a
  given OFF references. Tagged from the observation detail page (an on/off select, and a pair
  select of the observation's ON captures when a row is OFF) via `POST /captures/{id}/position` —
  HTML-only, like the M7 `kind` dropdown.
- **ON−OFF difference + classify-on-difference** (`confirm/onoff.py`,
  `confirm/classifier.py::classify_difference_npz`): `difference_spectrum(...)` divides the ON by
  the OFF in linear power (`method="ratio"`, the standard position-switch — cancels the receiver
  bandpass; `"subtract"` available), yielding a flat spectrum with the HI line as the only bump.
  It feeds the *existing* `classify_spectrum` (same baseline-fit + peak-in-Doppler-window + SNR),
  recorded under a distinct provenance name **`hline_v1_onoff`** with `ref_capture_id` + `method`
  in `params` (the single-capture `hline_v1` is untouched). Served at
  `GET /api/captures/{on_id}/difference` (MHz/v_LSR axis, OFF inferred from the pairing or `?ref=`)
  and `POST /api/captures/{on_id}/classify_difference`; the observation detail page shows a
  "Classify difference (ON−OFF)" action + verdict beside the single-capture one.

## v0.10.6 — 2026-07-14 — Waterfall: keep history across a window resize

Follow-up to `v0.10.5` — browser-only, no milestone, no schema change.

- **Resizing the window no longer clears the waterfall** (`static/waterfall.js`): when the pane
  height changes, the 1:1 history buffer is now carried over — existing rows are copied into the
  resized buffer **with no vertical rescale**, so every row keeps its exact pixels (verified: the
  retained rows are pixel-identical across a resize, residual 0 at shift 0). A taller pane adds
  empty room at the bottom that fills as new data arrives; a shorter one clips the oldest rows.
  `v0.10.5` rebuilt (blanked) the buffer on resize — but only the *per-frame* scroll ever needed
  to stay 1:1, so a one-time copy on resize is safe.

## v0.10.5 — 2026-07-14 — Waterfall: no per-row brightness flicker

Bugfix from live feedback (a screencast) — browser-only, no milestone, no schema change.

- **A row keeps its exact brightness as it scrolls** (`static/waterfall.js`): the history
  buffer was a fixed 320 rows scaled to the pane's pixel height with nearest-neighbour
  sampling. Because that ratio is non-integer, each data row was drawn 1px tall on some frames
  and 2px on others as it scrolled — so following a single point showed it brightening and
  dimming, and the whole waterfall read as random rather than a frozen-in-time record. The
  history is now rendered at the display's exact **vertical** resolution (one data row = one
  device pixel), so a scroll is an exact whole-pixel shift with no resampling — verified by
  reading the live canvas: the frame is a pixel-exact shift of the previous one (zero residual
  at the true offset). Only the horizontal `n_fft`→width scale remains, and it's fixed
  frame-to-frame so it never flickers. Waterfall history depth now follows the pane height
  (deeper on bigger/hi-dpi displays) instead of a fixed 320 frames. The dormant sub-frame
  smooth-scroll code (disabled in `v0.10.4`) is removed with this rework.

## v0.10.4 — 2026-07-14 — Steady waterfall + exact total-power readout

Follow-up to `v0.10.3` from live feedback — browser-only, no milestone, no schema change.

- **Waterfall is steady between frames** (`static/waterfall.js`): the sub-frame smooth-scroll
  *glide* is now off. Even crisp and integer-aligned (`v0.10.3`), the continuous inter-frame
  motion read as distracting "movement"; the waterfall now holds still between frames and
  advances one row when a new frame arrives. One-line toggle in the source restores the glide.
- **Exact total-power value on the strip** (`static/totalpower.js`): the current total power is
  now drawn prominently (bold, top-right of the strip) as well as in the status row, so the
  trace gives the trend and the number gives the exact value at a glance. (The narrow-range
  y-axis labels also carry a decimal now, so they no longer collapse to duplicates.)

## v0.10.3 — 2026-07-14 — Total-power strip + waterfall flicker fix

Between-milestones cockpit work — no milestone, no schema change (`user_version` stays 11),
no `install.sh`/`OS_IMAGE` change, no server change at all (browser-only). Two live-view items.

- **Total-power strip** (`static/totalpower.js`, `templates/index.html`, `static/style.css`):
  the classic single-dish total-power trace — integrated band power vs time — added to the live
  view under the waterfall, plus a `TP … dB` readout in the status row. It is what you watch
  during a Sun peak-up or a cold-sky-vs-ground check (total power rises/falls as a source
  crosses the beam) and the basis of a drift scan. Computed **client-side** from the frames the
  WebSocket already delivers — a numerically stable log-sum-exp mean of the PSD, in dB relative
  to the same reference as the spectrum — so it adds no backend and no extra Pi load, like the
  audio sonifier. Autoscaled, theme-aware, and it resets when the stream parameters change.
- **Waterfall flicker fixed** (`static/waterfall.js`): the sub-frame smooth-scroll used a
  *fractional* pixel offset, which re-sampled the nearest-neighbour row scaling onto a different
  grid every animation frame — a visible shimmer — and left a background gap at the top edge
  that grew then snapped shut each frame. The glide is now rounded to whole device pixels (crisp,
  still rows) and the strip the glide opens at the top is filled with the newest row instead of
  the background, so the leading edge is seamless. `prefers-reduced-motion` still disables the
  glide entirely.

## v0.10.2 — 2026-07-14 — Performance & resource efficiency

Between-milestones performance pass — no milestone, no schema change (`user_version` stays
11), no `install.sh`/`OS_IMAGE` change (so no QEMU gate). Four hot-path / resource fixes from
a review of the capture → DSP → server → SQLite pipeline on the Pi 5. **No behavior or
numerics change** — the DSP and npz paths are guarded by bit-identical / byte-identical tests.

- **Airspy live-view read is O(needed), not O(ring)** (`capture/airspy_cli.py`): `read()` no
  longer `np.concatenate`s the entire (~MBs, ~0.5 s) live-view ring every frame just to keep
  the newest ~64 KB. It now copies only the newest ring blocks that cover the request (a
  newest-first walk), cutting ~24 MB/s of wasted memory bandwidth on the real-Airspy path and
  shortening the lock hold that the pipe-drain thread contends for.
- **SQLite runs in WAL with tuned pragmas** (`db.py`): every connection comes up
  `journal_mode=WAL`, `synchronous=NORMAL`, `busy_timeout=5000`. Removes the per-commit fsync
  stall on the Pi's flash storage, lets readers proceed during a write, and waits out a
  contended write instead of raising `database is locked` (the server runs scheduler +
  tracking writer loops alongside every request). Adds `-wal`/`-shm` sidecar files next to the
  DB — data-dir tooling copies the whole directory, so nothing else changes.
- **npz captures stream to disk** (`capture/writer.py`): `NpzCaptureWriter` spills each
  spectrum row to a temp raw-`float32` file as it arrives and memmaps it into the `.npz` at
  close, instead of holding every `SpectralFrame` in RAM and `np.stack`-ing the whole history
  at close. Memory is now O(1) in capture length — a multi-hour unattended scheduler /
  drift-scan run no longer accumulates ~100 MB+/hour of live objects. The `.npz` output is
  byte-identical (a round-trip test guards it).
- **Welch window is cached, not rederived per frame** (`capture/dsp.py`): the Hann window is
  built once per `(window, n_fft)` and passed to `scipy.signal.welch` as an array rather than
  a string, skipping `get_window` + input revalidation each frame. Output is provably
  identical — a bit-for-bit test pins it against the string-window path, since the classifier's
  SNR thresholds depend on these exact dB values.

## v0.10.1 — 2026-07-13 — Argon ONE V5 case setup

Between-milestones convenience for the physical build — no milestone, no schema change
(`user_version` stays 11). Adds an installer switch to set up the Pi 5's Argon ONE V5 M.2
NVMe case. **`install.sh` changed**, so this release is gated on the QEMU install gate
before tagging. Nothing here touches the SDR/capture path or the bias-tee invariant.

- **`--install-argon`** (+ optional `--argon-nvme-boot`): a standalone installer action
  (exits after, like `--set-source`) that (1) idempotently enables the Pi 5 PCIe/M.2 slot
  in `config.txt` (`dtparam=pciex1` + `pciex1_gen=3`) so the NVMe is detected, and (2) runs
  Argon40's official V5 daemon installer (`argon1v5.sh`) for the case fan + power button.
  Pi 5-guarded (refuses other hardware without `--allow-unsupported-os`); reboot afterwards
  to bring up the slot. `--argon-nvme-boot` additionally sets an NVMe-first bootloader order
  (`BOOT_ORDER=0xf416`, `PCIE_PROBE=1`) via `rpi-eeprom-config`, confirmed on a TTY (or
  `--yes` on a non-TTY). Documented in `deploy/README.md`; `--help` updated. The embedded
  systemd/udev heredocs are untouched, so the CI drift check is unaffected.

## v0.10.0 — 2026-07-13 — M9 "Rotator — Discovery Drive"

Az/el rotator control for the KrakenRF Discovery Drive. Synthetic-first (built and
CI'd against an in-process simulator; the Drive is still on the wishlist). Four
pieces; schema advances to `user_version` 11. **`install.sh` changed** (a udev rule
for the Drive's USB-serial bridge), so this release passed the QEMU install gate.
Numbered `v0.10.0` — not `v1.1.0` — because no observing campaign has tagged
`v1.0.0` yet; the roadmap table shifts accordingly.

- **Rotator client + config + simulator** (#37): `astro/rotator.py` — a transport
  protocol with `RotctlTcpRotator` (rotctld NET protocol over TCP, stdlib socket),
  `EasyCommSerialRotator` (EasyComm II over USB-serial; pyserial), and `SimRotator`
  (finite slew rate under an injectable clock). `Station` gains the rotator config
  (kind none/sim/rotctl/easycomm + host/port/serial/baud + az/el limits + park).
  Migration 11; `+ pyserial`.
- **Slew + readback UI** (#38): `GET /api/rotator` status + `POST /rotator/slew|stop|
  park` + `POST /observations/{id}/slew_to_target` — every slew limit-checked and
  logged to the running observation. A rotator panel on `/station` (live readback)
  and a "Slew to target" button on the observation detail page.
- **Tracking mode + scheduler auto-slew** (#39): a lifespan loop re-points the
  rotator whenever the source drifts past a fraction of the beam from the last
  command (beam-crossing-aware); enabled per running observation, auto-stops when it
  ends. Scheduled captures auto-slew to their source at window open (best-effort).
- **Rotator MCP verbs + udev** (#40): `get_rotator_status` (read-only) and
  **`slew_rotator`** — the first MCP verb that moves hardware, hard-limit-checked and
  timeline-logged (never touches the bias tee). MCP grows to **22 tools**. udev rules
  for the Drive's USB-serial bridge (CP210x/CH340/Espressif → a stable
  `/dev/jansky-rotator` symlink).

## v0.9.1 — 2026-07-13

Maintenance release, no milestone — the shippable form of M8. The `v0.9.0` tag is
inert (it published nothing): its release run failed the CI **format check**
because six M8 files were lint-clean but not `ruff format`-clean. This release
applies `ruff format`, syncs `uv.lock` to the new version, and re-tags. No source
behavior changed; schema stays at `user_version` 10. See `v0.9.0` below for the
M8 feature notes.

## v0.9.0 — 2026-07-12 — M8 "Research bridge & guides"

The station's "data out" story and its first printable guides. Three pieces
shipped in this repo; schema advances to `user_version` 10. No `install.sh`
change (the migration runs on start). The fourth M8 piece — a pull skill that
lives in the `jansky-research` repo — is cross-repo and follows separately; the
jansky-observe side of that contract (the bundle format + the MCP tools it calls)
ships here.

- **Station UUID** (#31): a stable `Station.uuid` (UUID4) generated once at seed
  and backfilled onto existing stations by migration 10 — the station's permanent
  *machine* identity, distinct from the editable `name`, and jansky-research plan
  78's per-station key. Surfaced by `GET /api/station` + the `get_station_identity`
  MCP tool, shown on the `/station` page, and stamped into the PDF report footer.
- **Codified observation bundle** (#32): one documented JSON+npz export per
  observation (`export/bundle.py`, schema `jansky-observe.observation-bundle/1`) —
  averaged spectra with pointing, LST, timestamps, gain, cal-epoch reference,
  classifier verdicts, and the station UUID, exactly the format plan 78 consumes.
  Served at `GET /api/observations/{id}/bundle.json` (manifest) and `/bundle` (a
  zip of the manifest + one self-describing `capture-<id>.npz` per npz capture),
  via the `get_observation_bundle` MCP tool, linked from the observation detail
  page, and **embedded verbatim in the PDF report** so a report alone is
  machine-recoverable. The one-way Virgo/ezRA exporters stay strict third-party
  formats.
- **Guide PDFs** (#33): a **build guide** (authored from the plan's hardware chain)
  and an **observation guide** (generated from the seeded ObservationTypes, so it
  always matches the session wizard), both through the WeasyPrint pipeline at
  `GET /guides/{build,observation}.pdf` (index at `/guides`). WeasyPrint runs no
  JS, so the per-stage flow diagrams are deterministic inline SVG. Two house rules
  for every step-type guide PDF: every step gets a checkbox, and a build stage's
  diagram nodes are its checkbox parts. The build guide reinforces the bias-tee-OFF
  invariant with a safety callout.

The MCP surface grows to **20 tools** (`get_station_identity`, `get_observation_bundle`
added; still read + safe verbs only — no bias-tee control, no deletes).

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
