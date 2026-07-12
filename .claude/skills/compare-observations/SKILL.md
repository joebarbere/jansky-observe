---
name: compare-observations
description: Cross-session comparison of the same source over days or weeks — gather done observations via MCP, pull v_LSR spectra and classifier results per session, then run the four checks (stacking, SNR vs integration time √t law, peak-velocity stability, pointing repeatability) and append a markdown comparison table + findings to the newest observation. Use when asked to compare sessions, track a source over time, or check whether repeat observations are consistent.
---

# Compare observations: the same source, across sessions

One observation says what happened; a series says whether the **station** is behaving. This
skill compares repeat observations of the same source and turns the differences into
diagnoses — integration gains, calibration drift, pointing decay. The **hi-data-analyst**
agent is the persona; delegate the interpretation for a deep read.

## 0. Connect to the station MCP

The tools (`list_observations`, `get_observation`, `get_spectrum`, `get_capture_meta`,
`run_classifier`, `append_note`, …) come from the API server's MCP mount. If they aren't
available:

```bash
claude mcp add --transport http jansky-observe http://<pi>:8000/mcp
# default station hostname: http://raspberrypi.local:8000/mcp
```

## 1. Gather the series

- `list_observations(status="done")`, filtered to the source (ask which source if ambiguous).
  Two sessions is a comparison; one is not — say so and stop rather than padding.
- Per observation: `get_observation` (dialed az/el, times, SDR settings, notes),
  and per `.npz` capture `get_spectrum(capture_id, axis="vlsr")` + the `ClassifierResult`
  rows from `get_capture_meta`. **Run `run_classifier` on any unclassified capture first** —
  the comparison rests on classifier numbers, not eyeballed ones.
- Comparisons are only fair between like sessions: same axis (v_LSR), and note any SDR-setting
  differences (gain, sample rate) up front — they qualify every check below.

## 2. The four checks

1. **Stacking.** Average the per-session v_LSR profiles and check the combined SNR against
   expectation: N similar sessions should stack to ≈ √N × the single-session SNR. A stack that
   gains less than that means the sessions disagree — profile shifts or one RFI-polluted
   session dragging the average; identify which session by leave-one-out.
2. **SNR vs integration time — the √t law.** From each capture's elapsed integration time,
   the expected SNR ratio between sessions is √(t₂/t₁). Compare against the classifier SNRs.
   Deviations implicate **gain drift or RFI**, not statistics — say which session underperforms
   and check its `axis="mhz"` spectrum for fixed-frequency contamination.
3. **Peak-velocity stability.** Tabulate each session's peak v_LSR and its scatter. **Secular
   drift is a frequency-calibration alarm**: the topocentric correction moves ±30 km/s over
   the year and the LSR axis exists to remove it, so any residual session-to-session drift is
   **instrumental**, not celestial.
4. **Pointing repeatability.** Dialed vs computed az/el per session (computed via
   `get_pointing` for each session's time). Growing offsets across the series mean the
   pointing model is decaying — **recommend redoing the Sun pointing calibration**
   (ladder win #1, plan §5.4).

## 3. Write the comparison

A markdown note: a **comparison table** — one row per session: date, integration time,
classifier SNR + verdict (name + version), peak v_LSR, dialed-vs-computed Δaz/Δel — followed
by **findings**, one per check, each stating what the numbers show and what (if anything) to
do. `append_note` it to the **newest observation** in the series (and save a local file too,
if asked).

## 4. Provenance (plan §12.5)

Same rules as `/write-up`: every SNR, verdict, and peak velocity **cites its
`ClassifierResult` row** (name + version); uncertainty stated; **no unsupported "detection"
language** — `hline_v1` verdicts are threshold evidence only until `hi4pi_xcheck` arrives
(jansky-research plan 78), and a `detected` peak far from any plausible galactic v_LSR for
the pointing is written up as probable RFI (the 1421.25 MHz bare-input spur precedent).
The comparison interprets the classifier's numbers; it never overrides them.
