# M12 — v0.13.0 "Model overlay, radiometer SNR & noise diagnostics"

**Status:** proposed (spec). Not yet scheduled. Closes the **graph-output gaps** a comparison
against [Virgo](https://github.com/0xCoto/Virgo) surfaced: Virgo overlays the *expected* 21 cm
profile from a reference survey on your spectrum, reports a **radiometer-equation** sensitivity,
and shows a **total-power histogram** with a Gaussian fit. jansky-observe has richer station
management but none of those three analysis outputs. This milestone adds them — all **advisory
analysis, never verdicts** — staying inside the existing capture → observation → classifier model.

**Why now.** The model overlay is the single most confidence-building graph in amateur HI ("does my
bump match the model?"), and it is the visual half of the already-planned `hi4pi_xcheck` (plan §6).
The radiometer estimate became nearly free once **M10 put Tsys in the app** — with Tsys + bandwidth
+ integration time we can state the theoretical noise floor and "time-to-detect," which turns a
non-detection into an honest "under-integrated vs. genuinely absent" call. The histogram is a cheap
RFI/saturation tell.

**Honesty framing (the load-bearing constraint).** None of these three outputs is a detection
verdict — verdicts come **only** from the deterministic classifiers (provenance rule, plan §12.5).
The overlay is a *visual aid*; the radiometer number is an *estimate*; the histogram is a
*diagnostic*. The **quantitative** model cross-check (the v2 `hi4pi_xcheck` classifier that returns
a calibrated agreement verdict) stays **deferred to jansky-research plan 78** — this milestone
consumes that harness, it does not replace it (see the cross-repo boundary below).

**Scope guardrails.**
- **No change to the capture/SDR path, the daemon, or the bias-tee invariant.** New reductions are
  pure numpy with synthetic-fixture tests.
- **No schema change** — the overlay, the radiometer estimate, and the histogram are all computed
  on demand from existing rows (Tsys on the `CalibrationEpoch`, capture settings, the averaged
  spectrum); provenance rides the existing `ClassifierResult.params` / bundle JSON. `user_version`
  stays **14**.
- The one new external dependency is a **best-effort, cached network fetch** for the reference
  profile — `httpx` is already a dep (MCP); no new system package ⇒ **no `install.sh`/`OS_IMAGE`
  change ⇒ no QEMU gate.** The fetch degrades to "model unavailable" offline and never blocks a page.
- Everything new over MCP is **read-only**.

---

## Piece 1 — reference HI profile client (the seam to plan 78)

A thin, **pluggable** provider that returns the expected 21 cm profile for a pointing — velocity +
brightness-temperature arrays — so Pieces 2–3 can draw/compare it. The pluggability is what respects
the plan-78 boundary.

- **`astro/hi_reference.py`** (`ReferenceProfile` = `(v_lsr_kms[], t_b_k[], source, l_deg, b_deg)`):
  - `reference_profile(l_deg, b_deg, *, provider) -> ReferenceProfile | None` — dispatch to a provider.
  - **Provider A — `web` (default, needs network):** fetch the HI4PI (preferred) or LAB profile for
    (l, b) from a public query service, **cache to `data/hi_reference/<l>_<b>.npz`** (rounded to the
    grid), degrade to `None` offline/error. Best-effort — the overlay is optional context.
  - **Provider B — `bundle` (offline / authoritative):** read a profile handed over by
    **jansky-research plan 78's tool** (dropped into the data dir or carried on a returned bundle) —
    this is the science-grade path; observe just renders what plan 78's survey handling produced.
  - Rounded-(l, b) cache key so a re-render is instant and offline-repeatable.
- **Decision:** observe **never bundles the survey itself** (that is plan 78's job); it either
  fetches a single profile on demand or consumes one plan 78 supplies. The provider interface is the
  contract between the repos.

---

## Piece 2 — model overlay on the spectrum (the headline graph)

- **Plot** (`export/figures.py`): extend `profile_figure` (or a sibling `profile_overlay_figure`)
  to draw the reference `ReferenceProfile` **on the same v_LSR axis** as the observed averaged
  spectrum — observed as the solid trace, model as a scaled dashed overlay, both labelled. Because
  observe's spectrum is relative power (not K until flux-cal), the model is **shape-matched**
  (normalised to the observed peak/area) with an explicit caption: *"reference model (HI4PI/LAB),
  shape only — visual aid, not a detection verdict."*
- **API + UI:** `GET /api/captures/{id}/overlay?axis=vlsr` returns `(v_lsr, observed, model)` arrays
  (model `null` when unavailable); the capture detail page gains an **"Overlay reference model"**
  toggle beside the existing spectrum, and the PDF report shows the overlay when a model was
  obtained. 200 with `model: null` (never an error) when offline / off-plane / no pointing.
- **Provenance:** the overlay records `{survey, l_deg, b_deg, fetched_at}` in the render context and
  the observation bundle's per-capture block — machine-recoverable, but **no `ClassifierResult` row**
  (it is not a verdict).

---

## Piece 3 — radiometer-equation sensitivity (nearly free given M10 Tsys)

- **Reduction** (`confirm/radiometer.py`, pure numpy, `__all__ = ["radiometer_estimate"]`):
  ```
  radiometer_estimate(*, tsys_k, channel_bw_hz, integration_s, measured_peak_k=None,
                      target_snr=5.0, assumed_line_k=None) -> dict
  ```
  - `delta_t_rms_k = tsys_k / sqrt(channel_bw_hz * integration_s)` — the per-channel noise floor
    (the radiometer equation; `channel_bw_hz = sample_rate / n_fft`, `integration_s` from the
    capture's frame count / fps or elapsed).
  - `achieved_snr = measured_peak_k / delta_t_rms_k` (when a measured peak is supplied) — an
    independent cross-check on the classifier's empirical SNR.
  - `time_to_target_s = (tsys_k * target_snr / line_k)**2 / channel_bw_hz` for an `assumed_line_k`
    (galactic-plane HI ≈ 30–100 K) — "integrate this long to reach SNR 5"; makes a non-detection
    interpretable.
  - Returns those plus the inputs, JSON-able for the bundle.
- **Inputs** are all present: `tsys_k` from the capture's `CalibrationEpoch` (M10), bandwidth/
  integration from the capture settings. When no Tsys is on the epoch → return `available: false`
  with a note (needs a sky/ground calibration first), never an error.
- **API + MCP + UI:** `GET /api/captures/{id}/radiometer`; a small **"predicted noise / time-to-
  detect"** line on the capture detail + report next to the empirical SNR; a read-only
  `get_radiometer_estimate` MCP tool. Explicitly labelled an **estimate**, not a verdict.

---

## Piece 4 — total-power histogram + Gaussian fit (the noise diagnostic)

- **Reduction** (extend `confirm/` or `capture/dsp` helper, pure numpy): histogram the per-frame
  band-power series and fit a Gaussian; return `{mean, sigma, skew, excess_kurtosis, gaussian_p}`
  and a **non-Gaussianity flag** (skew/kurtosis beyond a threshold ⇒ likely RFI or an ADC-saturation
  tell — clean thermal noise is Gaussian).
- **Figure** (`export/figures.py::total_power_histogram_figure`): the histogram with the fitted
  Gaussian overlaid, the flag annotated. Added to the capture detail + the PDF report's diagnostics.
- Read-only; folds into the existing total-power data (M6 strip) — no new capture path.

---

## Stretch (ship only if cheap; else fast-follows)

- **Combined "observation summary" figure** — Virgo's one-shot multi-panel (overlay spectrum +
  waterfall + total-power series + histogram) as a single report page via matplotlib
  `subplot_mosaic`. Pure composition of Pieces 2/4 + existing figures.
- **All-sky HI pointing map** — the "small gap": an HI4PI moment-0 all-sky background on `/sky`
  (or `/maps`) with the current pointing marked, the way Virgo plots your position on the survey.
  Needs a bundled/fetched all-sky image (bigger asset) → likely a fast-follow, not core M12.

---

## Cross-repo boundary (explicit — this is the subtle part)

- **jansky-research plan 78** owns the **survey handling + the quantitative cross-check**: the v2
  `hi4pi_xcheck` classifier that returns a *calibrated agreement verdict* (statistical, with full
  provenance). That is the science and it stays there (plan §6: "the harness is built once there and
  consumed here").
- **jansky-observe (this milestone)** owns the **advisory outputs**: the visual overlay, the
  radiometer estimate, the histogram. It obtains the reference profile either by a convenience web
  fetch (Provider A) **or** from plan 78's tool (Provider B) — the `hi_reference` provider interface
  is the seam. Observe never emits a model-agreement *verdict*; it draws and estimates.

This keeps the provenance rule intact (verdicts only from the deterministic classifiers) while
giving the station tool the confidence-building graph now, in-app.

---

## Not in scope (parked)

- **Column density N_HI** — integrating the calibrated line to a real astrophysical number requires
  **absolute (K) flux/temperature calibration**, which is itself parked (maps/spectra stay relative
  until a flux-cal milestone). Note it as the natural sequel once absolute calibration lands.
- **FITS exporter** for the dynamic spectrum — Virgo writes FITS; observe uses SigMF/npz +
  Virgo-CSV/ezRA. Add a FITS exporter only if a collaborator needs it (one-way, like the others).
- **Pulsar / incoherent dedispersion** — Virgo supports giant-pulse/FRB search; a hydrogen-line
  station is a different instrument. Consciously out of scope.

---

## Tests (synthetic only, no hardware/sky/network)

- `astro/hi_reference.py`: Provider A with the fetch **mocked** (a canned HI4PI response → parsed
  `ReferenceProfile`; a network error → `None`, cached-file round-trip); Provider B reads a fixture
  npz. No real network in tests.
- `confirm/radiometer.py`: hand-computed `delta_t_rms_k` for known Tsys/bandwidth/integration;
  `time_to_target_s` monotonic in Tsys and target SNR; `available: false` when Tsys is missing.
- Histogram: Gaussian input → flag clear + fit recovers σ; an injected spike/skew → flag set.
- Overlay figure + API: model present → arrays aligned on the v_LSR axis + nonempty PNG; model
  absent → `model: null`, 200 not error; the report renders both cases.
- MCP: `get_radiometer_estimate` (+ `get_hi_model_overlay` if added) return the payloads read-only;
  the exact-tool-list test updated (→ **26–27 tools**).
- Coverage ≥ 85%; `ruff`/`mypy` clean; run **`make fmt`** before pushing (CI's `ruff format --check`
  isn't in `make lint`); `/verify` incl. the synthetic smoke green.

## Release

- **v0.13.0** (minor = milestone). **No schema change** (`user_version` stays 14). **No
  `install.sh`/`OS_IMAGE` change ⇒ no QEMU gate.** MCP grows by read-only tool(s) only. Update
  `CHANGES.md` (M12 section), `README.md` milestone table + feature list, `CLAUDE.md` status, and the
  vault runbook (a "compare your line to the model + how long to integrate" note). Flip the Virgo
  comparison: the overlay / radiometer / histogram gaps are closed; N_HI + FITS remain parked.

## Open decisions

1. **Reference survey + endpoint:** HI4PI (modern, preferred) vs LAB (what Virgo uses) as the
   Provider-A default; the exact public query endpoint (must verify one that works from the Pi, else
   ship Provider B — plan-78-supplied — first and make the web fetch the follow-up).
2. **Overlay normalisation:** shape-only (normalise model to the observed peak/area, honest while
   spectra are relative) vs. wait for absolute calibration to overlay in true K. Recommend
   shape-only now with the caption, upgrade when flux-cal lands.
3. **`assumed_line_k` for time-to-detect:** a fixed galactic-plane default (~50 K) vs. derive from
   the reference profile's own peak when a model was fetched. Recommend deriving from the model when
   present, else the default.
4. **Phasing:** Piece 3 (radiometer — pure, no network, immediately useful) + Piece 4 (histogram)
   first; Piece 1+2 (reference client + overlay) second, since they carry the network/boundary
   questions. Stretch pieces only if cheap.
