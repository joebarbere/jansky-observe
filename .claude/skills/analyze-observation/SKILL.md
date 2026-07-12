---
name: analyze-observation
description: Post-session analysis for an observation id — pull metadata and captures via MCP, run the deterministic v1 classifier (hline_v1) on every .npz capture, fetch the v_LSR spectrum, and interpret baseline quality, SNR, peak-velocity plausibility, and RFI signs into a markdown analysis note whose claims cite ClassifierResult rows. Use after a session, when asked to analyze, interpret, or grade an observation and its captures.
---

# Analyze an observation: verdicts from code, interpretation from Claude

For one observation id, end to end: classify → fetch profiles → interpret → write the note.
The **hi-data-analyst** agent is the persona behind this skill — delegate the interpretation
step to it for a deep read; it carries the station numbers and HI physics so they never need
re-explaining.

## 0. Connect to the station MCP

The tools (`get_observation`, `run_classifier`, `get_spectrum`, `get_capture_meta`,
`append_note`, …) come from the API server's MCP mount. If they aren't available:

```bash
claude mcp add --transport http jansky-observe http://<pi>:8000/mcp
# default station hostname: http://raspberrypi.local:8000/mcp
```

## 1. Pull the observation

`get_observation(observation_id)` — source and pointing, times, SDR settings, the **checklist
as performed** (who/when per tick), notes, and the capture list. Read the checklist and notes
as data-quality context: skipped items, late ticks, mid-session nudges, and troubleshooting
notes all belong in the analysis.

## 2. Classify every `.npz` capture

For each spectrum capture:

- `run_classifier(capture_id)` → a **`ClassifierResult`** row (classifier name **`hline_v1`**,
  version **1**): the verdict — `detected` (SNR ≥ 5), `uncertain` (2–5), `not_detected` —
  plus peak SNR, peak frequency/velocity, and baseline residual RMS. The classifier fits and
  subtracts a polynomial baseline (excluding the signal window), searches for a peak inside
  the LSR-corrected Doppler window (|v_LSR| ≤ 250 km/s), and thresholds on
  SNR = peak / baseline RMS (plan §6).
- `get_capture_meta(capture_id)` — integration time, sample rate, gain, FFT settings.

**Verdicts come only from the classifier.** Never assign, upgrade, or soften a verdict
yourself — the provenance rule (plan §12.5) is what makes the analysis citable.

## 3. Fetch the profile

`get_spectrum(capture_id, axis="vlsr")` — the averaged profile on the v_LSR axis (arrays +
rendered PNG). Pull `axis="mhz"` too when RFI is suspected: RFI lives at fixed topocentric
frequency, the line lives at fixed v_LSR.

## 4. Interpret — four questions per capture

1. **Baseline quality.** Compare the classifier's baseline residual RMS against the expected
   radiometer noise, ΔT/T ≈ 1/√(B·t) for the channel bandwidth and integration time from the
   capture meta. Residuals well above that expectation mean ripple or RFI is inflating the
   noise — and deflating the SNR the verdict rests on.
2. **SNR.** Quote the classifier's number. Is it plausible for the integration time — and
   would more integration plausibly move an `uncertain` over the line (SNR ∝ √t)?
3. **Peak v_LSR plausibility.** Galactic HI lives at **|v_LSR| ≲ 150 km/s**, and the velocity
   structure tracks galactic longitude (rotation-curve kinematics: which sign/spread is
   expected at this pointing?). A peak far outside that, or at a velocity the pointing can't
   produce, points at frequency calibration or RFI — not hydrogen.
4. **RFI contamination.** Narrow spikes, features that sit still in MHz but move in v_LSR
   between captures, baseline steps, excess residual RMS.

State uncertainty plainly at each step. When a spectrum looks wrong, suspect — in order —
RFI, baseline ripple, frequency calibration, pointing (the hi-data-analyst's ordering).

## 5. Write the analysis note

A markdown note, appended to the observation via `append_note` (and saved as a local file
too, if asked). House honesty standards (plan §12.5):

- Every claim **cites its `ClassifierResult` row** — classifier name + version + SNR — or a
  specific spectrum/plot.
- **Uncertainty stated**; caveats (RFI, short integration, baseline residuals) carried with
  the numbers they qualify.
- **No unsupported detection language.** "Detected" appears only when a `ClassifierResult`
  says `detected`; everything else is described as what it is.

## 6. What v1 does — and doesn't — establish

`hline_v1` verdicts are **threshold evidence only**: a peak of sufficient SNR inside the
Doppler window. They do not establish that the *profile shape* matches the sky. The
quantitative sky-match confirmation — `hi4pi_xcheck` (v2), cross-correlating the observed
profile against the HI4PI survey at the same pointing — is **not yet available**: it arrives
with jansky-research plan 78, which builds the comparison harness once (house-tested there,
consumed here). Say so in the note whenever a verdict might be read as more than it is.
