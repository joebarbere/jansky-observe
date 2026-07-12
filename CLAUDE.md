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

M0–M5 have all shipped — `v0.6.0` is live on the station and **feature-complete against
the plan**: capture (synthetic + real Airspy), observation records + wizard + observing
ladder, the hline_v1 classifier + live badge + LSR axes, PDF reports + photos + Virgo/ezRA
exporters, Stellarium view-slew + cross-check, HackRF RFI sweep, gpsd locations, 17 MCP
tools, and every plan-§12.6 Claude deliverable. **v1.0.0 is not a feature: it is tagged
after one real end-to-end observing campaign (plan → observe → confirm → PDF), from
whatever `v0.x` is current** — prerequisites are physical (feed chain connected, Sun
pointing calibration run). M6 (v0.7.0, the station cockpit) is the next milestone. The `hi4pi_xcheck` (v2 confirmation) still arrives via jansky-research
plan 78. The HI4PI cross-check (`hi4pi_xcheck`, v2) remains **deferred to
jansky-research plan 78** per plan §6 — the comparison harness is built once there and
consumed here. The full plan lives in `plans/jansky_observe.md` — read it before any
feature work.

**M6 (v0.7.0 "station cockpit") is in progress** — see `plans/roadmap-post-v0.6.md`. Landed
so far:
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

Still to come in M6: spectrum audio, dark mode, localization, archive/soft-delete
(`/new-migration`), FPS knob, RFI-survey template.

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
