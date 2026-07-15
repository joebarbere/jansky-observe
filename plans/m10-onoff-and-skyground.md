# M10 — v0.11.0 "Position switching (ON/OFF) & sky/ground calibration"

**Status:** proposed (spec). Not yet scheduled. Closes the gaps the beginner HI runbook
surfaced: the app confirms HI by a polynomial **baseline fit on a single ON capture**
(`hline_v1`) and has no concept of ON/OFF position switching, no way to difference two
captures, and no in-app sky/ground (Y-factor / Tsys) number. This milestone adds those,
staying inside the existing capture → observation → classifier model.

**Why now.** ON/OFF (position switching) is the textbook amateur-HI confirmation and what the
vault notes assume; the baseline fit is a valid *substitute*, but an independent ON−OFF result
is a genuinely stronger, second confirmation, and the sky/ground ΔdB is the runbook's
"permanent system-health number" (Milestone 7/9) that today lives only in jansky-research.

**Scope guardrails.** Read-and-reduce only: **no change to the capture/SDR path, the daemon, or
the bias-tee invariant** (`HLINE_AIRSPY` / `airspy_cli.py` untouched). No `install.sh`/`OS_IMAGE`
change ⇒ **no QEMU gate** at release. New reductions are pure numpy with synthetic-fixture tests.
Everything new over MCP is **read-only** (the `slew_rotator` exception stands alone).

Schema advances `user_version` **11 → 12** (one migration via `/new-migration`).

---

## Piece 1 — ON/OFF capture roles + pairing (gap #1)

`Capture.kind` (`science`/`ref_load`/`cold_sky`/`hot_ground`) is about *calibration measurement
type* and must not be overloaded — an ON or an OFF pointing is still a `science` capture. Add an
**orthogonal position** instead.

- **Model / migration 12** (`models.py`, `db.py`):
  - `Capture.position: str = "on"` — allowed `("on", "off")`. Default `"on"` so every existing
    and future single-pointing capture is unaffected. Add `CAPTURE_POSITIONS = ("on", "off")`.
  - `Capture.pair_capture_id: int | None` (FK `capture.id`, indexed, nullable) — set on the
    **OFF** capture, pointing at the ON it references. `None` for unpaired captures.
  - Guard both columns on `PRAGMA table_info` (migration 1 builds the latest schema), matching
    every prior additive migration.
- **Tagging** (HTML-only, mirrors the M7 `kind` dropdown on the observation detail page):
  - A `position` select (on/off) next to the existing `kind` select per capture row.
  - When set to `off`, a second select lists the observation's `on` captures to pick the ON it
    pairs with → writes `pair_capture_id`. `POST /captures/{id}/position` (form).
- **Not exposed as a new capture `kind`**; not a new MCP verb (tagging is HTML-only, like M6/M7
  kind/archive). Read surface: `GET /api/captures/{id}` already returns the row — include
  `position` + `pair_capture_id`.

---

## Piece 2 — ON−OFF difference spectrum + classify-on-difference (gaps #2, #3)

A new pure reduction, then reuse the existing classifier on its output.

- **Reduction** (`confirm/onoff.py`, pure numpy, `__all__ = ["difference_spectrum"]`):
  ```
  difference_spectrum(on_freq, on_db, off_freq, off_db, *, method="ratio")
      -> (freq_hz, diff_db)
  ```
  - Require identical axes (same `center_freq_hz`/`sample_rate_hz`/`n_fft`) — the same
    `(center, rate, n_fft)` key the live average uses. Mismatch → `ValueError` → 422 at the route.
  - `method="ratio"` (default, the standard position-switch): `on_lin/off_lin` cancels the
    receiver bandpass frequency response (the whole point of an OFF); return
    `linear_to_db(on_lin / off_lin)` — a ~flat spectrum with the HI line as the only bump.
  - `method="subtract"`: `linear_to_db(maximum(on_lin - off_lin, floor))` — for when the OFF
    is a poor bandpass match; documented as the weaker option.
  - Reuses `confirm/baseline.db_to_linear` / `linear_to_db`.
- **Classify on the difference:** feed the difference straight into the existing
  `classify_spectrum(freq_hz, diff_db, *, window_hz)` — its baseline-fit + peak-in-Doppler-window
  + SNR verdict all apply unchanged (the difference is already bandpass-flat, so the baseline fit
  becomes a near-no-op residual). **Decision (recommended): a distinct provenance name**
  `hline_v1_onoff` (new `CLASSIFIER_NAME`-style constant) so `ClassifierResult` rows are honest
  about the method, with `params` carrying `ref_capture_id`, `method`, peak freq, SNR — satisfies
  the provenance rule (plan §12.5) without touching the single-capture `hline_v1`.
- **API** (`routers/captures.py`, mirrors the existing spectrum/classify routes):
  - `GET /api/captures/{on_id}/difference?ref={off_id}&axis=mhz|vlsr` → `(freq, power)` of the
    difference, same shape as `/spectrum` (so `waterfall.js`/the plot render it unchanged). 404
    if either file purged; 422 on axis mismatch or if `ref` isn't an `off` for this observation.
  - `POST /api/captures/{on_id}/classify_difference` (body/query `ref={off_id}`) → runs the
    reduction + `classify_spectrum`, appends a `ClassifierResult` (name `hline_v1_onoff`), renders
    a verdict plot (reuse `_plot_path`/the existing PNG renderer), returns the htmx fragment.
  - If `pair_capture_id` is already set on the OFF, `ref` may be omitted and inferred.
- **UI** (observation detail): when the observation has an ON with a paired OFF, add a
  **"Difference (ON−OFF)"** panel — the difference spectrum plot (v_LSR/MHz axis toggle) and a
  **"Classify difference"** button showing the `detected`/SNR verdict beside the single-capture
  one. Purely additive to `observation_detail.html` + `_capture_results.html`.
- **Frequency switching (gap #3), stretch:** same machinery once axes are aligned — the "OFF" is
  a same-pointing capture retuned by Δf; align by shifting the reference axis by the
  center-frequency delta before differencing. Ships **only if cheap**; otherwise a fast-follow.
  The `method`/reduction code is shared.

---

## Piece 3 — In-app sky/ground ΔdB + Tsys on the calibration epoch (gap #4)

The `cold_sky` / `hot_ground` capture kinds already exist (M7) and already attach to a
`CalibrationEpoch`. Add the reduction the runbook wants (the "log this ΔdB" system-health
number, Milestone 7/9) as first-class, trendable epoch fields — no new table.

- **Reduction** (`confirm/skyground.py`, pure numpy):
  ```
  sky_ground_delta(cold_db, hot_db, *, t_hot_k=300.0, t_cold_k=10.0)
      -> {"delta_db": float, "y": float, "tsys_k": float}
  ```
  - `delta_db = 10*log10(mean(hot_lin)/mean(cold_lin))` — band-mean total-power ratio.
  - `y = mean(hot_lin)/mean(cold_lin)`; `tsys_k = (t_hot_k - y*t_cold_k)/(y - 1)` (the Y-factor
    Tsys, with the documented assumed hot/cold temperatures the note already cites: ground ≈
    300 K, cold sky ≈ 5–10 K).
- **Model / migration 12** (same migration as Piece 1): `CalibrationEpoch.sky_ground_delta_db:
  float | None` and `CalibrationEpoch.tsys_k: float | None` — computed and stored when the epoch
  has both a `cold_sky` and a `hot_ground` capture.
- **API + UI:** on the `/calibration` page (and the "Calibration sweep" / "Tsys sky/ground pair"
  observation), a **"Compute Tsys / sky-ground ΔdB"** action that finds the epoch's cold_sky +
  hot_ground captures, runs the reduction, stores `delta_db`/`tsys_k`, and shows them. The
  calibration page's epoch list gains a ΔdB/Tsys column → the weekly-cadence **trend** the
  runbook asks for. Surfaced in the PDF report's Calibration section.
- **MCP (read-only):** extend the existing calibration read tool (or add `get_calibration_health`)
  to return the per-epoch `tsys_k`/`delta_db` series — lets `/compare-observations` and the
  analyst agent trend system health.

---

## Not in scope (fast-follows / parked)

- **ON/OFF-aware live average (gap #6):** the live accumulating average / HI badge stays
  single-pointing; the operator still hits "Reset avg" between ON and OFF. A guided live ON/OFF
  mode (accumulate ON and OFF separately, show the live difference + a live difference-SNR) is a
  natural M10.1 but not required for the recorded-capture workflow above.
- **HI4PI cross-check (gap #5):** unchanged — arrives via jansky-research plan 78, consumed here
  (plan §6, follow-up list). Independent of this milestone.
- **Rotation-curve / ezRA reduction (gap #7):** stays in jansky-research.

---

## Tests (synthetic only, no hardware/sky)

- `confirm/onoff.py`: injected line in ON, flat OFF → `difference_spectrum(ratio)` recovers a
  bump at the injected bin; identical ON==OFF → flat (no false bump); axis mismatch → `ValueError`.
  Use `/synthetic-fixture` for the ON/OFF `.npz` pair.
- `classify_difference`: an ON/OFF pair with a strong injected line → `hline_v1_onoff` verdict
  `detected` with SNR above 5; a no-line pair → `not_detected`. Byte-level `ClassifierResult`
  provenance (name/version/`ref_capture_id`) asserted.
- `confirm/skyground.py`: known hot/cold power ratio → expected `delta_db`, `y`, `tsys_k`
  (hand-computed); monotonicity checks.
- `db.py`: migration 12 round-trip — fresh DB has the new columns; a v11 DB upgrades and keeps
  data (the standard `test_migration_*` pattern); `user_version` ends at 12.
- Routes: `/difference`, `/classify_difference`, `/position`, the calibration compute action —
  happy path + 422 (axis mismatch / bad pair) + 404 (purged file).
- Coverage stays ≥ 85%; `ruff`/`mypy` clean; `/verify` (incl. the synthetic smoke) green. Run
  `make fmt` before pushing (CI's `ruff format --check` isn't in `make lint`).

## Release

- **v0.11.0** (minor = milestone). Schema `user_version` 12. **No `install.sh`/`OS_IMAGE` change
  ⇒ no QEMU gate.** MCP grows by the read-only calibration-health tool (→ 23 tools) if added;
  no new mutating verbs. Update `CHANGES.md` (M10 section), `README.md` milestone table + feature
  list, `CLAUDE.md` status. The runbook's "How jansky-observe confirms" note flips from "doesn't
  yet compute the ON−OFF difference" to "classify the ON−OFF difference directly."

## Open decisions

1. **Classifier name for the difference:** distinct `hline_v1_onoff` (recommended, cleanest
   provenance) vs. reuse `hline_v1` with `params.method="on_off"`. Distinct name means one more
   `CLASSIFIER_*` constant + the `dsp-reviewer`/provenance tests know both.
2. **`method` default:** `ratio` (bandpass-dividing, standard) vs. `subtract`. Recommend `ratio`
   default, `subtract` available.
3. **Assumed `t_cold_k`:** 10 K flat, or derive from pointing/season. Flat 10 K to start (the note
   already treats cold sky as ~5–10 K); refine later.
4. **Phasing:** Piece 1+2 first (the ON/OFF ask), Piece 3 second (independent), #3 freq-switching
   only if cheap.
