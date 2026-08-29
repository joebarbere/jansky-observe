# v0.16.0 — "The calibrated overlay: the model as a layer, and a residual in kelvin"

**Status:** spec + implementation (this branch). Closes an M12 shipped-vs-spec gap and adds the
one thing that turns the reference overlay from an impression into a number.

## Why now

M12 (`v0.13.0`) shipped the LAB reference overlay. Its own spec (Piece 2,
`plans/m12-model-overlay-and-radiometer.md`) said:

> the capture detail page gains an **"Overlay reference model"** toggle beside the existing
> spectrum

What actually shipped is a `target="_blank"` link to `/api/captures/{id}/overlay.png` in
`_capture_results.html`. The check exists; it is not in anybody's way. **A check you have to
remember to run is a check that does not run.**

The prompt for revisiting this is an outside data point. Pierre Terrier's 2025 *Galactic rotation
curve at 21 cm* (3 m amateur dish, France — <https://smallrt.blogspot.com/2026/01/galactic-rotation-curve-at-21-cm.html>)
overlays the LAB profile on **every one of its sixteen spectra**, always visible, never opt-in.
Reading that document it is possible to see — without any of their raw data — that their brightness
temperatures run at roughly 0.65 of LAB across all longitudes, consistent with an uncorrected
main-beam efficiency, and that their terminal velocities are correspondingly ~12 km/s low where the
profile has a faint high-velocity wing. The always-on overlay is what makes that visible.

It is also what our overlay **cannot** show, and that is the more important half of this work. The
M12 figure plots observed **relative power (dB)** against model **brightness temperature (K)** on
two independent twin axes — the docstring says "shape only" and means it. A scale error of 0.65 is
invisible to a shape comparison: the curves line up nicely and you conclude all is well. To see it
the two traces have to share one axis in kelvin.

M10 already put the missing ingredient in the app: `CalibrationEpoch.tsys_k` from the sky/ground
Y-factor (`confirm/skyground.py`). With Tsys and a baseline fit we can put the observed spectrum on
a kelvin axis, subtract the model, and report the scale ratio as a number.

## Honesty framing (load-bearing)

Unchanged from M12 and non-negotiable: **this is advisory analysis, never a verdict.** Verdicts come
only from the deterministic classifiers (plan §12.5). Three specific commitments:

- The kelvin axis is **antenna temperature `T_A`** unless a main-beam efficiency is configured. At
  the default `eta_mb = 1.0` the axis is labelled `T_A (K)`, not `T_B`. Calling `T_A` a brightness
  temperature is precisely the error that makes a 0.65 scale factor look like a physical result.
- The scale ratio is reported, never applied. We do not rescale the observed trace to make it match
  the model — that would hide the very defect the panel exists to surface.
- The quantitative model cross-check (`hi4pi_xcheck`, a calibrated agreement *verdict*) stays
  **deferred to jansky-research plan 78**. This ships the measurement, not the verdict.

## Scope guardrails

- **No schema change.** `user_version` stays **14**. Tsys is already on `CalibrationEpoch`;
  main-beam efficiency arrives as a `JANSKY_OBSERVE_ETA_MB` setting, not a column — a station
  constant with a documented default beats a migration for a number that is measured once.
- **No change to the capture/SDR path, the daemon, or the bias-tee invariant.** Every new reduction
  is pure numpy over an already-written `.npz`.
- **No new dependency, no `install.sh`/`OS_IMAGE` change ⇒ no QEMU gate.**
- Everything new over MCP is read-only; no new MCP verb (the existing `get_hi_model_overlay`
  gains fields).
- Additive API only — existing `overlay` JSON keys keep their meaning so the PDF report and the
  MCP tool do not move.

---

## Piece 1 — the kelvin axis (`confirm/tbscale.py`)

A pure function turning a relative-dB spectrum into antenna temperature, given Tsys.

```
T_A(v) = tsys_k * (P(v) / B(v) - 1)      # B = fitted baseline in linear power
T_B(v) = T_A(v) / eta_mb
```

The baseline is fitted with the existing `confirm/baseline.fit_baseline` over channels **outside**
the HI Doppler window, so the line cannot be absorbed into it. The physical assumption is the
standard single-load one: line-free channels carry `Tsys` alone, and excess power above the
baseline is the line. That assumption is stated in the docstring, because it is the thing that
breaks first (a receiver with a non-flat or drifting gain across the band violates it).

- `brightness_temperature(freq_hz, power_db, *, tsys_k, exclude, eta_mb=1.0, order=3)` →
  `TemperatureScale`.
- `TemperatureScale` carries the array plus `tsys_k`, `eta_mb`, `baseline_rms`, and the derived
  `is_main_beam` (False at `eta_mb == 1.0`) / `axis_label` (`"T_A (K)"` or `"T_B (K)"`) / `peak_k`,
  so callers label the axis from the data rather than guessing. `baseline_rms` is carried because a
  poor baseline fit is the usual reason a kelvin axis should not be trusted, and it would otherwise
  be invisible to everything downstream.
- Raises `ValueError` on non-positive `tsys_k` / `eta_mb` or mismatched axes, and propagates
  `fit_baseline`'s too-few-channels error.

## Piece 2 — the comparison (`confirm/overlay.py`)

Pure numpy, no IO. Interpolates the model onto the observed velocity grid over their overlap and
returns a `ModelComparison`:

- **`scale_ratio`** — least squares `observed ≈ a · model` through the origin. **The headline.**
  Terrier's would read ≈ 0.65. A station whose calibration is right reads ≈ 1.
- `residual_rms_k` — RMS of `observed − model` over the overlap.
- `peak_dv_kms` — observed peak velocity minus model peak velocity. A secular drift here is a
  frequency-calibration alarm, which is exactly what `/compare-observations` already looks for.
- `overlap_kms`, `n_channels` — so a comparison over four channels cannot masquerade as a result.
- `observed_peak_k`, `model_peak_k` — the two peaks the ratio reconciles, so a reader can check
  the ratio against them without re-deriving it.
- `v_lsr_kms`, `observed_k`, `model_k`, `residual_k` — the overlap-restricted arrays the figure
  draws, with the model already interpolated onto the observed grid.

Returns `None` when the overlap is shorter than `MIN_OVERLAP_CHANNELS` (8) rather than reporting a
ratio fitted to nothing.

## Piece 3 — the figure (`export/figures.py`)

Extend `profile_overlay_figure` with two optional keywords, both defaulting to the current
behaviour so existing call sites and tests are untouched:

- `observed_t_b_k=None` — when given, observed and model are drawn **on one shared kelvin axis**
  (no twinx), which is what makes a scale difference visible. Paired with
  `observed_axis_label` (default `"T_A (K)"`), passed through from
  `TemperatureScale.axis_label` so the figure cannot claim a `T_B` it was not given.
- `comparison=None` — when given, adds a lower residual panel (`observed − model`, K) with a zero
  line, captioned with the scale ratio and residual RMS.

The "visual aid, not a detection verdict" caption stays in both modes.

## Piece 4 — API + the toggle (the M12 gap)

- `_overlay_for_capture` gains a `calibrated` block: `{available, reason}` or `{temperature_k,
  tsys_k, eta_mb, is_main_beam, axis_label, baseline_rms, comparison}`. Degrades with a reason (no cal epoch / no Tsys / baseline
  fit failed) — never raises, never blocks the shape-only overlay.
- `GET /api/captures/{id}/overlay.png?calibrated=1&residual=1` — query params select the mode;
  `calibrated=1` with no Tsys returns **409 with the reason**, not a silently shape-only plot.
- `GET /captures/{id}/overlay_panel` — an htmx fragment: the image plus the comparison numbers plus
  **two** checkboxes, calibrated and residual. There is deliberately no "model off" box: a figure
  with the model hidden is just the spectrum plot, which already exists.
- `_capture_results.html`: the new-tab link becomes a `<details>` that loads the panel on first
  open (`hx-trigger="toggle once"` — an observation with a dozen captures must not fire a dozen LAB
  fetches unasked). Calibrated+residual default on whenever Tsys is available — the M12 spec's
  toggle, with the default set so the check runs itself.
- `static/overlay.js` — ~30 lines: rebuild the `<img src>` from the checkbox state. No framework.

## Testing

- `tests/test_tbscale.py` — a synthetic spectrum with a known injected line at a known Tsys
  recovers its amplitude in K; `eta_mb` divides; bad inputs raise.
- `tests/test_model_comparison.py` — **the regression that matters: a spectrum scaled by 0.65
  returns `scale_ratio ≈ 0.65`.** That is the Terrier defect encoded as a test. Plus a shifted
  spectrum returns the right `peak_dv_kms`, and a too-short overlap returns `None`.
- `tests/test_overlay.py` — extended for the new figure modes, the query params, the 409, and the
  panel fragment.

## Done when

- The overlay is an inline panel on the observation detail page, not a new-tab link, and within it
  calibrated mode is on by default whenever a Tsys exists.
- A capture with a sky/ground Tsys shows observed and model on one kelvin axis, a residual panel,
  and a scale ratio.

**An honest note on how far this gets.** The argument above is that always-on beats opt-in, and
what ships is still one click: the `<details>` starts collapsed and fetches on first open. That is
a deliberate trade — an observation with a dozen captures would otherwise fire a dozen LAB fetches
on page load — but it is a trade, not the ideal. Terrier's overlay is genuinely always visible and
ours is not. Closing the remaining gap means caching profiles per pointing rather than per capture,
so a whole observation costs one fetch; that is worth doing and is not done here.
- `make lint typecheck cov` green at the 85% floor; `/verify` passes.
- `CHANGES.md` + `CLAUDE.md` updated; no schema change; no installer change.

## Deliberately not in scope

- **Applying** the scale ratio. Reported only. See the honesty framing.
- A measured `eta_mb`. Measuring main-beam efficiency needs a calibrator source of known flux
  (Cas A / the Sun); this ships the knob and the honest `T_A` label, and the measurement is its own
  piece of work.
- `hi4pi_xcheck` — still jansky-research plan 78.
