---
name: write-up
description: Draft the narrative for an observation's PDF report or a vault research note — pull the observation and classifier results via MCP (running hline_v1 first on any unclassified capture), write the plan-§7 sections in house honesty standards, append the draft to the observation notes so the report includes it, then build the PDF with build_report. Use when asked to write up an observation, draft its report, or turn a session into a research note.
---

# Write up an observation: the narrative behind the PDF

For one observation id: gather → draft → append → build. The draft is the **narrative** the
WeasyPrint report (plan §7) wraps around the data — the report template already renders the
metadata block, weather, checklist, plots, photos, and capture inventory; this skill writes the
prose that makes them a story someone can trust. The **hi-data-analyst** agent is the persona —
delegate the interpretation section to it for a deep read.

## 0. Connect to the station MCP

The tools (`get_observation`, `get_capture_meta`, `run_classifier`, `append_note`,
`build_report`, …) come from the API server's MCP mount. If they aren't available:

```bash
claude mcp add --transport http jansky-observe http://<pi>:8000/mcp
# default station hostname: http://raspberrypi.local:8000/mcp
```

## 1. Gather — observation and verdicts

- `get_observation(observation_id)` — source and pointing, observation type, times, observers,
  SDR settings, weather snapshot, the **checklist as performed** (who/when per tick), notes,
  and the capture list.
- Per `.npz` capture: `get_capture_meta(capture_id)` for the `ClassifierResult` rows and
  integration/gain/FFT settings. **If a capture has no classifier result, run
  `run_classifier(capture_id)` first** — never write up unclassified captures without saying
  so explicitly in the draft.
- If `/analyze-observation` already produced an analysis note, read it — the write-up narrates
  the same evidence; don't re-derive or contradict it.

## 2. Draft the narrative — sections mirroring the report (plan §7)

1. **What was attempted.** Observation type and where it sits on the observing ladder
   (plan §5.4) — "HI pointed — Cygnus region, ladder win #2", not just a name. Target, planned
   window, what success would look like for this rung.
2. **Conditions.** The weather snapshot at start, and the checklist **as performed** — call out
   skipped or late items and anything the notes flag mid-session; they are data-quality caveats,
   not housekeeping.
3. **What the instruments recorded.** Captures (format, duration, size) and the SDR settings
   they ran under (sample rate, gain, center frequency). Dialed vs computed az/el.
4. **What the classifier found.** The verdicts, **citing each `ClassifierResult` row
   explicitly** — classifier name + version + SNR + verdict (e.g. "`hline_v1` v1: SNR 6.3,
   `detected`") — and the plots they rest on. This section reports; it does not argue.
5. **Interpretation** — clearly labeled as such. Baseline quality, SNR plausibility for the
   integration time, peak-v_LSR plausibility for the pointing, RFI signs. This is where Claude's
   judgment lives, attributed as judgment (plan §12.5).
6. **Next steps.** More integration (SNR ∝ √t — say what an `uncertain` needs), a repeat at the
   same pointing, an RFI sweep, a Sun-cal redo — concrete, tied to what the evidence showed.

## 3. Honesty rules (hard — plan §12.5)

- Every claim **cites its `ClassifierResult` row** (name + version + SNR + verdict) or a
  specific spectrum/plot. **Uncertainty stated**, caveats carried with the numbers they qualify.
- **No unsupported "detection" language.** `hline_v1` verdicts are **threshold evidence only** —
  a peak of sufficient SNR inside the Doppler window. The quantitative sky-match
  (`hi4pi_xcheck`) is **not yet available** (jansky-research plan 78), so no write-up may claim
  the profile matches the sky. Say so wherever a verdict might be read as more.
- A `detected` verdict whose peak sits **far from any plausible galactic v_LSR** for the
  pointing is written up as **probable RFI**, verdict notwithstanding — this station has
  precedent: the **1421.25 MHz bare-input spur** from the build's QA passed a naive peak test
  while being pure receiver artifact. Cite that precedent when invoking it.

## 4. Append the draft

`append_note(observation_id, text)` with the full markdown draft. This is what makes step 5
work: the report template renders observation notes, so **the narrative must be a note before
the PDF is built** — a draft that only exists in chat never reaches the report.

## 5. Build the PDF

`build_report(observation_id)` (REST: `POST /api/observations/{id}/report`) → the WeasyPrint
PDF at `data/observations/<id>/report.pdf`, downloadable from the observation page. If the
user wants a **vault research note** instead of (or besides) the PDF, write the same draft to
the file they name — same sections, same honesty rules.
