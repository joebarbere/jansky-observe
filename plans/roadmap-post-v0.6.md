# jansky-observe roadmap after v0.6 — M6–M12 and the follow-up list

Companion to `plans/jansky_observe.md` (which defines M0–M5, all shipped, and the v1.0.0
gate). This document organizes everything queued after feature-complete into milestones
aligned to versions, per the 2026-07 planning session. Same semver rule: **minor =
milestone, patch = fixes between milestones**. All of M6–M8 can be built while waiting for
first light; none of it moves the v1.0.0 gate, which remains *one real end-to-end campaign
on a v0.6.x-or-later install*.

| Version | Milestone | Theme |
|---|---|---|
| v0.7.0 | M6 | Station cockpit — the UI tells you everything at a glance |
| v0.8.0 | M7 | Calibration & scheduling — what the research plans actually need |
| v0.9.0/.1 | M8 | Research bridge & guides — data out, documentation out |
| v0.10.0 | M9 | Rotator — Discovery Drive support (shipped as v0.10.0, not the v1.1.0 this table first guessed) |
| v0.11.0/.1 | M10 | Position switching (ON/OFF) & sky/ground Tsys — spec `plans/m10-onoff-and-skyground.md` |
| v0.12.0 | M11 | HI mapping — raster & drift sky maps — spec `plans/m11-hi-mapping.md` |
| v0.13.0 | M12 | Model overlay, radiometer SNR & noise diagnostics — spec `plans/m12-model-overlay-and-radiometer.md` |
| v1.0.0 | — | Unchanged: tagged after one real campaign, from whatever v0.x is current |

M9 shipped ahead of the v1.0.0 campaign, so the whole post-M8 line renumbered onto the v0.x
track (M9 = v0.10.0, M10 = v0.11.0, M11 = v0.12.0, M12 = v0.13.0); v1.0.0 stays the campaign
gate, not a feature. M10, M11, and M12 each carry a standalone spec file (linked above) rather
than a section here — this document's detailed sections cover M6–M9.

---

## M6 — v0.7.0 "Station cockpit"

Everything a glance at the main page should answer, plus the operator-comfort items.

### Status bar (base template, every page)

- **Clocks**: UTC, station local time, and **LST** (astropy, from the active station's
  location) — LST is the one an observer actually schedules by.
- **Station chip**: active station name + current pointing offsets (Δaz/Δel from the Sun
  cal, or "uncalibrated").
- **Source badge**: synthetic / airspy / hackrf, from the daemon status over the control
  channel, with the daemon's fps and frame age (stale frames → badge goes amber).
- **Weather chip**: current conditions from the existing provider, cached ~15 min.
- **Disk gauge**: free/total for the data dir, plus *estimated hours of SigMF capture
  remaining* at the current rate; amber < 20 %, red < 10 %.

### Diagnostics endpoint + MCP tool

`GET /api/diagnostics` and a matching `get_diagnostics` MCP tool (18th tool) returning the
debug bundle a remote Claude needs for this class of setup, in check order:

1. systemd unit states (server + capture daemon),
2. SDR USB enumeration (Airspy / HackRF vendor:product present?),
3. daemon status + age of the last ZMQ frame (is data flowing?),
4. thermals: `vcgencmd measure_temp` + `get_throttled` decoded (undervoltage / soft-limit
   flags — this Pi brushed the soft limit before the Argon ONE V2 case added active cooling;
   the undervoltage flags still matter regardless),
5. disk free for the data dir; best-effort SMART summary (NVMe yes, SD no),
6. DB `PRAGMA user_version` vs expected,
7. last N error lines from the two units' journals (best-effort, needs journal access).

Rewire `/troubleshoot-chain` to call it first — the decision tree keeps its physical steps
(injector current, bias-tee *checked never changed*), but every software question becomes
one tool call.

### Audio: listen to the station

A toggle in the live view that **sonifies the spectrum stream client-side** (WebAudio from
the frames the WebSocket already delivers — no new backend, no extra Pi load). Raw IQ
audio would need a new high-rate stream and would sound like undifferentiated hiss;
sonification of the 4 fps spectra is both cheaper and better listening. Modes:

- **Receiver** — spectrally-shaped noise: the live PSD drives a filter bank over pink
  noise, so the band literally *sounds* like its shape; RFI spurs become audible tones.
- **Doppler pitch** — the LSR Doppler window mapped to a musical pitch range; the HI
  peak's velocity becomes pitch, its SNR becomes loudness. A detection *sings*.
- **Geiger** — click rate follows live-badge SNR; the classic "counter" aesthetic.
- **Drone** — a slow ambient pad whose harmonics are weighted by band power; the
  leave-it-on-in-the-background mode.

All synthesis parameters (base pitch, scale, decay) are client-side constants — an
aesthetics pass, not a science surface. The waterfall stays the quantitative view.

### The rest of the cockpit

- **Dark mode toggle** (CSS custom properties + `prefers-color-scheme` default + explicit
  toggle persisted in localStorage). The waterfall palette gets a dark-friendly variant.
- **Localization**: dates/times/numbers rendered in the browser's locale and timezone by
  default (client-side `toLocale*`), with a settings override (station-local or UTC-only
  display for lab-notebook consistency). UTC stays canonical in the DB and all exports.
- **Archive observations**: soft-delete — an `archived_at` column (migration via
  `/new-migration`), hidden from lists by default, restorable, **HTML-only, never exposed
  over MCP** (same principle as photo delete). Separate explicit action to purge a
  capture's *files* (the 43 GB/h SigMF reclaim path) while keeping the DB row + provenance.
- **FPS surfaced**: document the `--fps` knob in the env file; optional cosmetic
  smooth-scroll interpolation in the canvas. Rationale recorded: each row is an integrated
  spectrum, so 4 fps is a spectrometer cadence, not a rendering limit — raising it trades
  integration per row for motion.
- **RFI survey template**: a seeded "RFI survey @ 1420" ObservationType whose checklist
  drives the existing HackRF `rfi_sweep`, with before/after sweep comparison summarized in
  the observation (and hence the report).

---

## M7 — v0.8.0 "Calibration & scheduling"

The milestone the jansky-research station track (plans 78/79/80/84) is actually waiting
for. Their requirements, translated:

### Calibration captures (plans 78, 79)

- A calibration kind on captures: **reference load (50 Ω)**, **cold sky**, **hot ground**,
  each a short averaged-spectra capture with the same metadata as science captures.
- **Cal-epoch provenance**: every science capture records which calibration epoch it falls
  under; plan 79's weekly cal cadence becomes a first-class object, not a naming
  convention. Exposed in exports and the PDF report.
- A guided calibration checklist in the wizard (mirroring the Sun-cal pattern from §5.4).

### Scheduler (plans 79, 84 + the session-timer idea)

- **Session timer**: elapsed/remaining display on a running observation.
- **Scheduled starts**: "start capture N minutes before <source> transit, run M minutes,
  attach to observation X" — transit times already come from `astro/pointing.py`. An
  in-app scheduler table (survives restarts; the daemon stays the only SDR owner).
- Daily-cadence schedules (plan 79 needs a year of daily spectra; plan 84 needs the daily
  solar transit) — a repeat rule, not just one-shots.
- Unattended-run guardrails: a scheduled capture that would fill the disk past the red
  threshold refuses to start and says why.

### Drift-scan campaign mode (plan 80)

- Fixed-pointing continuous capture with **sidereal-day tagging** so passes stack; a
  campaign row grouping the passes; disk-budget-aware chunking (npz per pass, IQ optional).

### Sky chart (the in-UI Stellarium answer)

- A canvas alt/az sky chart: horizon, galactic plane, seeded catalog sources, Sun/Moon,
  and the dish's current pointing cone (21° beam). All positions from the astropy code we
  already have; fully offline. Desktop Stellarium keeps its role (M5 cross-check) — this
  is the always-available glanceable version. (Stellarium itself can't be embedded
  headlessly: it needs a GL context and its RemoteControl API serves state, not frames.)

---

## M8 — v0.9.0 "Research bridge & guides"

### Data out (plan 78's input format)

- **Station UUID**: migration adds a stable UUID to Station; stamped into every export,
  report, and MCP identity response. (Also the future multi-station identity — see
  follow-ups.)
- **Codified observation bundle**: one documented export (JSON + npz) carrying averaged
  spectra with pointing, LST, timestamps, gain settings, cal-epoch reference, classifier
  results, and station UUID — exactly the "averaged-spectra format from the station's
  capture service" plan 78 consumes. The PDF report embeds the same data block so a report
  alone is machine-recoverable.
- **jansky-research pulls via MCP** (decision 2026-07-12): a skill *in the jansky-research
  repo* that calls this station's MCP (`list_observations`, `get_spectrum`,
  `export_capture`, `get_capture_meta`) to fetch bundles into `data/` there. jansky-observe's
  side of the contract is: the bundle format above + making sure those tools expose it.

### Guides as PDFs

- **Station build guide** and **observation guide**, rendered through the existing
  WeasyPrint pipeline:
  - build stages get **mermaid diagrams per stage**, nodes labeled;
  - diagram nodes map to a **parts list, each entry checkbox-prefixed** so printouts are
    check-off-able;
  - **every step in any guide PDF gets a checkbox** — the house rule for all future
    step-type PDFs.
- Content sources: the plan's hardware sections, the wizard checklists, and the
  jansky-research station docs (`station/hydrogen-line-receiver.md`).

---

## M9 — v0.10.0 "Rotator" (Discovery Drive)

Target hardware: the KrakenRF **Discovery Drive** (the Discovery Dish's own az/el rotator,
ESP32-S3). Its two control surfaces, per KrakenRF's docs:

- **rotctl (Hamlib) protocol over NET TCP** — the Drive natively accepts rotctld-style
  network commands; GPredict/SatDump/Look4Sat connect this way.
- **EasyComm II over USB-serial** (hamlib model 202, 19200 baud) as the wired fallback.

Both are simple text protocols, so the plan is a small native client (no hamlib binary
dependency on the Pi). **Decisions (2026-07-13):** build **both transports** in M9 (adds a
`pyserial` dep + a udev rule for the USB-serial device), and **expose a guarded `slew_rotator`
MCP tool** alongside read-only status — a deliberate departure from the prior "no moving
hardware from an LLM" default, gated behind hard az/el-limit checks + timeline logging (the
absolute bias-tee invariant is untouched). Since no campaign has tagged v1.0.0 yet, M9 ships as
**v0.10.0**.

- `astro/rotator.py`: rotctl-TCP client (position set/get, stop) + EasyComm II serial variant +
  an in-process `SimRotator` (M9 is synthetic-first until the Drive is on the roof); Drive
  host/port/serial + az/el limits + park position on the Station record (migration 11).
- Wizard/detail integration: **slew to target**, readback vs astropy-expected az/el
  (pointing offsets applied), drift **tracking mode** (periodic re-point at
  beam-crossing-aware cadence).
- Safety rails: configurable az/el limits, a stow/park action, never slew without an
  explicit operator action or an enabled schedule, and slews always logged to the
  observation timeline.
- MCP: `get_rotator_status` read-only **plus a guarded `slew_rotator` verb** (decided
  2026-07-13, above) — the first mutating MCP verb that moves hardware, so it enforces the
  station az/el limits, refuses outside them, and logs every slew to the observation timeline.

Version note: numbered v1.1.0 assuming the campaign has tagged v1.0.0 by then; M9 in fact
landed first and shipped as **v0.10.0** (the table above is reconciled to what actually shipped).

---

## M10 — v0.11.0 "Position switching (ON/OFF) & sky/ground Tsys" — shipped

Closes the ON/OFF and Tsys gaps the beginner HI runbook surfaced. Full spec and status:
**`plans/m10-onoff-and-skyground.md`**. Shipped v0.11.0 (ON−OFF difference + `hline_v1_onoff`,
schema 12) and v0.11.1 (sky/ground ΔdB + Tsys on the calibration epoch, schema 13). Read-and-reduce
only; no capture/SDR/bias-tee change; one read-only MCP tool (→ 23).

## M11 — v0.12.0 "HI mapping — raster & drift sky maps" — shipped

The "can I image with the dish?" answer: turn a set of pointed captures (a rotator-driven raster
grid **or** ingested M7 drift passes) into a coarse 2-D map of galactic HI intensity / v_LSR /
total power — beam-limited to the ~21° HPBW, and labelled as such. Promotes the parked *drift-scan
sky maps* follow-up (plan §11) now that M9 can drive a grid. Full spec: **`plans/m11-hi-mapping.md`**.
Read-and-reduce + a raster runner that moves hardware only through M9's already-guarded `slew`
primitive (no new device path, no new mutating MCP verb, no `install.sh` change ⇒ no QEMU gate);
schema 13 → 14. Shipped v0.12.0.

## M12 — v0.13.0 "Model overlay, radiometer SNR & noise diagnostics" — proposed

Closes the three graph-output gaps a comparison against [Virgo](https://github.com/0xCoto/Virgo)
surfaced: a **reference HI-profile overlay** (the expected 21 cm profile from the LAB/HI4PI survey
drawn on your spectrum — the visual half of the deferred `hi4pi_xcheck`), a **radiometer-equation
SNR / time-to-detect** (nearly free now that M10 put Tsys in the app), and a **total-power histogram
+ Gaussian fit** (an RFI/saturation noise diagnostic). All three are **advisory analysis, never
verdicts** — the quantitative model cross-check stays deferred to jansky-research plan 78; a
pluggable `hi_reference` provider (best-effort LAB web fetch **or** a plan-78-supplied profile) is
the cross-repo seam. Full spec: **`plans/m12-model-overlay-and-radiometer.md`**. Read-and-reduce +
one best-effort cached network fetch; **no schema change** (`user_version` stays 14), **no
`install.sh`/`OS_IMAGE` change ⇒ no QEMU gate**, read-only MCP only. Not yet scheduled.

---

## Follow-up list (explicitly not scheduled)

- **v2 / multi-station** — station discovery (mDNS), peer identity (the M8 UUIDs),
  cluster data sharing, and **incoherent combining** of simultaneous observations
  (√N sensitivity by stacking independent spectra). Parked 2026-07-12: not yet clear the
  research value justifies the work for incoherent-only combining. Two notes for whenever
  this revives: (1) true *coherent* interferometry requires shared clock/LO and
  sample-synchronous capture — the Airspy Mini has no external clock input, so that path
  is hardware-gated (a KrakenSDR is a 5-channel coherent receiver; jansky-research plan 83
  is the matching science slice); (2) the jansky library v0.2.0 now ships an
  `interferometry.py` module — evaluate it first.
- **hi4pi_xcheck (v2 classifier)** — still arrives via jansky-research plan 78, consumed
  here (plan §6).
