# M11 — v0.12.0 "HI mapping — raster & drift sky maps"

**Status:** proposed (spec). Not yet scheduled. Turns a *set of pointed captures* into a coarse
2-D **map** (intensity / velocity / total power) of extended emission — the "can I make images
with the dish?" answer. Promotes the parked follow-up *"drift-scan sky maps (ezGal-style)"*
(plan §11 / `roadmap-post-v0.6.md`) into a real feature now that the rotator (M9) can drive a
grid and campaigns (M7) already tag drift passes.

**Why now / honesty up front.** The 0.7 m dish has a **~21° HPBW** at 21 cm (`astro/pointing.py:
hpbw_deg`). It is a single-pixel instrument — no focal-plane array, no snapshot image, and
**nothing smaller than the beam is resolvable** (the Moon is 0.5°). What *is* 21°-scale and bright
at 1420 MHz is the **Milky Way's neutral hydrogen**, so the achievable product is a low-resolution
**map of galactic HI intensity and v_LSR** (and a total-power map for the Sun/galactic plane) —
"weather map," not "telescope photo." This milestone builds exactly that, and **labels its own
resolution** so the output can't be mistaken for resolved structure. The mount does not improve
resolution (that's dish diameter); it only automates coverage.

**Scope guardrails.**
- **No change to the capture/SDR path, the daemon, or the bias-tee invariant** (`HLINE_AIRSPY` /
  `airspy_cli.py` untouched). The reduction is pure numpy with synthetic-fixture tests.
- The raster runner (Piece 3) **moves hardware only through the already-guarded `slew` primitive**
  (`server/rotator.py`, limit-checked, timeline-logged) and starts/stops captures over the
  **existing control channel** exactly like the M7 scheduler — **no new device path, no new
  mutating MCP verb.** Map data over MCP is **read-only** (the `slew_rotator` exception still
  stands alone).
- **No `install.sh` / `OS_IMAGE` change ⇒ no QEMU gate** at release.

Schema advances `user_version` **13 → 14** (one migration via `/new-migration`).

---

## Piece 1 — the SkyMap grouping + grid geometry (model, migration 14)

A map is a set of captures, each at a known pointing, grouped under one grid definition. This is
structurally the M7 `Campaign` idea (fixed record + captures tagged in) plus grid geometry, so add
a sibling table rather than overloading `Campaign` (a campaign is *one* fixed pointing; a map is
*many*).

- **`SkyMap` table** (`models.py`): `id`, `name` (indexed), optional `source_id` (the map center,
  FK `radio_source.id`, nullable — a blank-sky galactic strip has no source), `frame`
  (`"azel"` | `"galactic"`, default `"galactic"` — HI maps are naturally l/b), grid geometry
  (`center_l_deg`/`center_b_deg` **or** `center_az_deg`/`center_el_deg`, `extent_x_deg`,
  `extent_y_deg`, `step_deg` — default `step_deg ≈ 10.0`, ~half-beam Nyquist-ish sampling),
  `dwell_s` (per-cell integration, default 60), `metric` default (`"hi_intensity"` |
  `"peak_vlsr"` | `"total_power"`), `status` (`"active"` | `"done"`), `notes`, `created_at`.
- **`Capture.sky_map_id: int | None`** (FK `sky_map.id`, indexed, nullable). Captures already carry
  their commanded/actual pointing (`pointing_az_deg`/`el`, `computed_az_deg`/`el`) — the map cell
  is *derived* from that pointing at reduce time, so no per-cell row/col column is needed. (Store
  nothing the pointing doesn't already give us.)
- **Migration 14** (`_migration_14_sky_map`, via `/new-migration`): create `sky_map`; add
  `capture.sky_map_id` under the standard `PRAGMA table_info` guard + its index. Additive; every
  existing capture stays `sky_map_id = NULL`.

---

## Piece 2 — the map reduction + heatmap render (the deliverable; ships without hardware)

The heart of the milestone, and deliberately **independent of acquisition** — it reduces *any* set
of positioned captures, so it's fully synthetic-testable and useful even for manually-pointed or
drift maps before the drive exists.

- **Reduction** (`confirm/mapping.py`, pure numpy, `__all__ = ["cell_value", "grid_map"]`):
  - `cell_value(freq_hz, power_db, *, metric, window_hz)` → one scalar per capture:
    - `"hi_intensity"` — baseline-subtracted **integrated line flux** in the LSR Doppler window
      (reuse `confirm/baseline` fit + trapezoid over the window). Units: arb. (∝ K·km/s).
    - `"peak_vlsr"` — v_LSR of the peak (reuse `astro/lsr` + the classifier's peak search).
    - `"total_power"` — band-mean power (continuum: Sun, galactic plane).
  - `grid_map(positions, values, *, extent, step, frame)` → `(grid_2d, x_axis, y_axis, mask)`:
    bin `(x_deg, y_deg, value)` onto the regular grid; **beam-weighted** accumulation (each sample
    smears over ~HPBW, so a Gaussian-of-HPBW kernel is the honest interpolant, not nearest-cell).
    `mask` marks **unvisited cells** — they are returned as gaps (NaN), **never silently
    interpolated across** (house honesty rule; the render shows them hatched).
- **Render** (`export/figures.py`, matplotlib → PNG, same pipeline the reports use):
  a heatmap with axes in the map's frame (galactic l/b or az/el), a colorbar (intensity/power) or
  a **diverging** colormap for `peak_vlsr`, optional contours, and a **mandatory beam-resolution
  annotation** (a 21° HPBW circle + a "resolution ≈ 21°; each pixel is a beam-smoothed average"
  caption). Undersampled/missing cells render hatched.
- **Provenance:** the map's parameters (metric, grid spec, `frame`, HPBW, the list of contributing
  `capture_id`s + each cell's value) are the map's record — stored on/derived from the `SkyMap`
  and its captures, and emitted in the JSON (Piece 4). No `ClassifierResult` row (a map isn't a
  detect/not-detect verdict), but the same provenance discipline (plan §12.5): the image is
  reproducible from the listed captures + params.

---

## Piece 3 — acquisition: the raster runner + drift/campaign ingest

Two ways to *fill* a map's cells; the reduction above doesn't care which.

- **Raster runner** (`server/mapping.py`, modeled on `server/tracking.py` + the M7 scheduler):
  a lifespan-style loop over an **active** `SkyMap`. Pure decision `next_map_cell(state) ->
  Cell | None` (tested: returns the next unvisited in-limits cell, or `None` when the grid is
  covered) + async `map_tick` (IO): **slew to the cell via the guarded `slew` primitive**
  (`server/rotator.py` — 422 out of limits, logged), settle, start a `dwell_s` capture over the
  control channel, stop + register it tagged `sky_map_id` (+ the pointing it was taken at), advance.
  Guardrails, all reused: **disk-red refusal** (`would_exceed_disk_red`) before each cell,
  out-of-limits cells **skipped + logged once** (not fatal), a transport error **aborts and marks
  the map `done`**. Needs a **running observation + a configured rotator** (same preconditions as
  tracking). HTML-only control: `POST /maps/{id}/start` / `/stop`. The daemon stays the only SDR
  owner (the runner drives it, never opens the SDR).
- **Ingest existing captures** (no hardware): `POST /maps/{id}/ingest` groups a **drift-scan
  campaign's** passes (M7) or any selected pointed captures into the map (sets `sky_map_id`). A
  strip of fixed-el drift scans at stepped declinations *is* a raster once ingested — this is the
  no-drive path, and the reason Piece 2 is acquisition-agnostic.
- **No new mutating MCP verb.** Starting a raster is browser-triggered, exactly like tracking
  start; it reaches hardware only through the pre-existing guarded slew.

---

## Piece 4 — surfacing: map page, API, MCP, report, bundle

- **UI:** a `/maps` index + `/maps/{id}` detail — grid/coverage status (cells visited / total),
  the rendered heatmap with a **metric toggle** (intensity / velocity / total power) and a
  **frame toggle** (l-b / az-el), start/stop (raster) + ingest controls, and the contributing
  capture list. Nav link "Maps".
- **API:** `GET /api/maps[/{id}]` (grid spec + per-cell values + coverage, JSON) and
  `GET /api/maps/{id}/image.png?metric=…&frame=…` (the heatmap). 404 on a purged contributing
  file is tolerated (that cell → gap).
- **MCP (read-only):** `list_sky_maps` + `get_sky_map` (grid, metric, coverage, per-cell values,
  HPBW) → the analyst agent / `/compare-observations` can read a map. **No mutating map verb**
  (→ 24 MCP tools if both added; keep to one, `get_sky_map`, if minimizing surface → 24).
- **Report + bundle:** the map heatmap embeds in the **PDF report** (a "Sky map" section, with the
  resolution caption) and the **M8 observation bundle** carries the map manifest (grid + cell
  values + capture refs) so a map is machine-recoverable and ezRA-ingestible.

---

## Not in scope (fast-follows / parked)

- **Quantitative rotation-curve fit** — stays in **jansky-research / ezRA** (M10 parked #7). This
  milestone produces the *gridded map image + data*; the tangent-point rotation-curve science
  consumes the bundle downstream. Keep that boundary.
- **Mosaicking multiple maps / all-sky stitching** — later; one grid per `SkyMap` here.
- **On-the-fly (continuous-scan) mapping** — the runner is **point-dwell-capture** (stop-and-stare),
  which the 4 fps spectrometer cadence + a coarse grid make adequate. Continuous slew-while-integrate
  (with per-sample position tagging) is a real upgrade but a separate effort.
- **Absolute temperature (K) calibration of the map** — depends on the M10 Tsys / a future
  flux-cal milestone; maps ship in **relative** intensity/power (matching the v1 "relative
  power/SNR only" rule, plan §11). The `peak_vlsr` map *is* already physical (km/s).
- **Resolution beyond the beam** — not possible with one 0.7 m dish; interferometry is the parked
  v2 / multi-station + KrakenSDR path (`roadmap-post-v0.6.md` follow-ups). Say so in the docs;
  don't imply the map can be sharpened.

---

## Tests (synthetic only, no hardware/sky)

- `confirm/mapping.py`: a synthetic field (a Gaussian HI blob placed at a known l/b across a set of
  fixture captures) → `grid_map` recovers the blob at the right cell, beam-smoothed; unvisited cells
  come back NaN in `mask` (no silent fill); `peak_vlsr` cells recover the injected Doppler shift;
  `total_power` tracks injected band power. Use `/synthetic-fixture` for the positioned `.npz` set.
- `next_map_cell`: covers a grid in order, skips out-of-limits cells, returns `None` when done
  (pure, exhaustive).
- `map_tick` / runner: with a `SimRotator` + synthetic daemon, a small grid runs end to end, each
  cell slews within limits + registers a tagged capture; a disk-red state refuses; an out-of-limits
  cell is skipped + logged; a transport error aborts to `done`.
- `export/figures.py`: the heatmap renders for each metric (byte-nonempty PNG), carries the HPBW
  annotation, and hatches masked cells.
- `db.py`: migration 14 round-trip — fresh DB has `sky_map` + `capture.sky_map_id`; a v13 DB
  upgrades and keeps data (standard `test_migration_*` pattern); `user_version` ends at 14.
- Routes: `/maps`, `/maps/{id}`, `image.png`, `start`/`stop`/`ingest` — happy path + 409
  (no rotator / no running observation on `start`) + 404 (purged file → gap, not error).
- Coverage stays ≥ 85%; `ruff`/`mypy` clean; `/verify` (incl. the synthetic smoke) green. Run
  **`make fmt`** before pushing (CI's `ruff format --check` isn't in `make lint`).

## Release

- **v0.12.0** (minor = milestone; M11 is the first milestone past M10). Schema `user_version` 14.
  **No `install.sh`/`OS_IMAGE` change ⇒ no QEMU gate.** MCP grows by read-only map tool(s) only;
  no new mutating verb. Update `CHANGES.md` (M11 section), `README.md` milestone table + feature
  list, `CLAUDE.md` status. Flip the runbook's "can I image?" answer from "not built yet" to
  "raster/drift HI maps in-app (beam-limited ~21°)."

## Open decisions

1. **New `SkyMap` table vs. extend `Campaign`.** Recommend a new table — a campaign is one fixed
   pointing, a map is a grid; overloading muddies the M7 drift semantics. Ingest bridges the two.
2. **Default frame.** `galactic` (l/b) — HI maps live there and it's what ezRA expects — vs. `azel`
   (what the rotator commands). Recommend `galactic` default with an az/el toggle; store both the
   commanded az/el (on the capture) and the derived l/b (at reduce time).
3. **Interpolant.** Beam-weighted Gaussian (HPBW kernel, honest) vs. nearest-cell (blocky but makes
   no smoothness claim). Recommend Gaussian **with** the resolution annotation, masked gaps kept.
4. **MCP surface.** `get_sky_map` only (24 tools) vs. add `list_sky_maps` too. Recommend both read
   tools; still zero mutating map verbs.
5. **Phasing.** Piece 1 (model) → **Piece 2 (reduction + render, the deliverable — ships and demos
   on synthetic/manual captures with no drive)** → Piece 3 (raster runner + ingest) → Piece 4
   (surfacing). Piece 2 is the value; Piece 3 is what the drive unlocks.
