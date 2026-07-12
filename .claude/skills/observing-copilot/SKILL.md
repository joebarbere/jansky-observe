---
name: observing-copilot
description: The during-session companion at the dish — polls get_live_status and the live HI badge over MCP, reports the running-SNR trend, warns when pointing drift approaches half the beam (HPBW ≈ 21°, nudge at ~10°) with minutes-to-nudge from the drift rate, answers "is that spike RFI or the line?" (Doppler window + persistence across frames), and appends timestamped notes on request. Use while an observation is running.
---

# Observing copilot: Claude at the dish

The browser shows the waterfall; this skill handles the reasoning around it. It is a
**human-driven loop** — check when asked ("check again") or on a cadence the observer sets,
and keep each report short: the numbers, the trend, and what (if anything) to do.

## 0. Connect to the station MCP

The station tools (`get_live_status`, `get_observation`, `get_pointing`, `append_note`, …)
come from the API server's MCP mount. If they aren't available in this session, add the
server first:

```bash
claude mcp add --transport http jansky-observe http://<pi>:8000/mcp
# default station hostname: http://raspberrypi.local:8000/mcp
```

## 1. Load the running session

- `get_live_status` — the live SNR badge, current az/el, elapsed time, frame freshness.
- `get_observation` for the running observation (find it via
  `list_observations(status="running")` if no id was given): the source, the **dialed az/el**
  the dish was pointed to, planned window, and the checklist as performed so far.

Confirm with the observer that this is the session they mean before monitoring it.

## 2. The monitoring loop

On every "check again" (or each tick of an agreed cadence):

- `get_live_status` + **the live HI badge** — the running SNR computed on the server-side
  accumulating average spectrum (`get_live_status` carries it; the raw surface is
  `GET /api/live/hi_badge`). Badge thresholds match the classifier: `detected` SNR ≥ 5,
  `uncertain` 2–5, `not_detected` below.
- Report the **trend, not just the number**. Rising roughly as √t is healthy integration.
  Flat after a rising start means the average has stopped gaining — check drift (step 3) or
  new RFI polluting the average. Falling means something changed: pointing walked off, gain
  changed, RFI turned on.
- If the observer nudges the dish or changes anything mid-session, offer to **reset the
  badge accumulator** (the badge's reset endpoint) so the running SNR reflects the new
  configuration instead of averaging across two pointings.

## 3. Pointing drift: when to nudge

The dish is fixed alt-az; the sky drifts through the beam.

- `get_pointing(source_id)` — where the source is *now* (az/el + drift rate in °/min).
- Compare to the observation's **dialed az/el**. HPBW ≈ 21°, so the source leaves the beam
  when the total offset reaches ~half the beam: **warn as |Δ| approaches ~10°, and say when
  to nudge**: minutes-to-nudge ≈ (10° − |Δ|) / drift rate. A source near transit at dec δ
  crosses the whole beam in ≈ 21° / (15°·cos δ per hour) — hours, not minutes, which is what
  makes attended drift observing practical.
- After a nudge: record the new dialed az/el in a note (step 5) and offer the badge reset
  (step 2).

## 4. "Is that spike RFI or the line?"

Two tests, in this order:

1. **Frequency.** The HI line can only live inside the Doppler window — within
   **±~1.2 MHz of 1420.406 MHz topocentric** (|v_LSR| ≤ 250 km/s at this pointing/time).
   Anything outside that window is not hydrogen, full stop.
2. **Persistence.** The line is broad (tens of km/s) and **persists across frames**,
   strengthening in the accumulating average. Narrowband spikes — even inside the window —
   that flicker frame to frame, or appear and vanish, are RFI; so are features that track
   local activity (a device switched on) rather than the sky.

Say which test the feature failed. If the question is "why is there *nothing*" rather than
"what is this spike" — flat waterfall, dead noise floor, no response to pointing — that's a
chain fault: hand off to `/troubleshoot-chain`.

## 5. Notes on request

When the observer says "note that" (or something noteworthy happens and they agree):
`append_note(observation_id, text)`, timestamped. Note-taking discipline: **what was seen +
what was done** — "SNR 3.1 and rising at 20:41; Δaz 8.7°, nudged to az 212 / el 44, badge
reset". Notes are the session's memory; the badge won't remember why it was reset.

## Provenance

The badge and classifier verdicts are **code output** (classifier name + version, running on
the server). The copilot **interprets — trend, drift, RFI judgment — and never overrides**:
if the badge says `uncertain`, the session note may argue the profile looks promising, but
the verdict stays the badge's. Interpretation lives in notes, clearly attributed (plan §12.5).
