---
name: plan-session
description: "What should I point at tonight?" — plan an observing session against the live station via MCP (whats_up + weather, filtered to the dish's 0–70° elevation limit, ranked by the observing ladder, transit timing, beam-crossing duration, and the weather window), then pre-fill a draft Observation so the wizard is ready at the dish. Use when picking a target, planning tonight's session, or asked what's worth observing.
---

# Plan a session: what should I point at tonight?

Runs from any machine on the LAN against the station's MCP surface. The output is a
recommendation table **and a draft Observation** — walk to the dish with the wizard pre-filled.

## 0. Connect to the station MCP

The station tools (`whats_up`, `get_weather`, `create_observation_draft`, …) come from the API
server's MCP mount. If they aren't available in this session, add the server first:

```bash
claude mcp add --transport http jansky-observe http://<pi>:8000/mcp
# default station hostname: http://raspberrypi.local:8000/mcp
```

## 1. Ask the station what's up and what the weather is

- `whats_up(window)` — sources above the horizon over the session window (default: the next
  ~4 h from now), with current az/el, transit time, and drift rate per source.
- `get_weather` — conditions now + the forecast window (NWS).

Ask the user for the session window if they didn't give one ("tonight" ≈ dusk to bedtime).

## 2. Filter: the dish's hard limits

- **Elevation 0–70°** — the manual alt-az mount can't point higher; drop any source whose
  usable time falls outside that band for the whole window.
- A source *transiting* above 70° may still work on either side of transit — say so rather
  than silently dropping it.

## 3. Rank the survivors

Rank by, in order:

1. **The observing ladder (plan §5.4).** If the station's pointing offsets are still
   **0.0 / 0.0** (`pointing_offset_az_deg` / `pointing_offset_el_deg` on the Station — no
   completed "Sun pointing calibration" observation in `list_observations`), recommend
   **Sun pointing calibration FIRST** and say why: every pointed az/el the server displays is
   only as good as the Δaz/Δel pointing model, and the Sun cal is the ladder's win #1 and the
   prerequisite for everything pointed. Otherwise the ladder order is: HI pointed — Cygnus
   region → HI pointed — rotation-curve longitudes → continuum drift scan (Cas A / Cyg A).
2. **Transit timing** — a source transiting inside the session window beats one setting or
   still rising; note the transit time per candidate.
3. **Beam-crossing duration** — HPBW ≈ 21°, so a fixed pointing holds a source for
   ≈ 21° / (15°·cos δ per hour): ~1.8 h for Cyg A, ~2.7 h for Cas A. Longer crossing = more
   integration without nudging the dish.
4. **The weather window** — wind matters on a 700 mm dish and rain matters on the operator;
   clouds are irrelevant at 21 cm. Recommend the calm/dry stretch, not just "now".

## 4. Present a recommendation table

One row per viable candidate, recommendation on top:

| Target | Type (ladder rung) | Az/el now | Transit | In 0–70° until | Beam-crossing | Weather window | Verdict |

Follow with one short paragraph: the pick and the reason (ladder position, timing, weather).

## 5. Create the draft — tick nothing

Once the user accepts a target:

- `create_observation_draft` with the recommended source, ObservationType, and planned window.
- Do **not** call `tick_checklist_item`. The checklist belongs to the human at the dish —
  each tick persists *what was physically done, by whom, when*, and none of it has happened
  yet. The draft's job is only to have the wizard pre-filled.

## 6. Hand off

End with the link: the draft is waiting in the session wizard at **http://\<pi\>:8000**
(linked from the home page). Walk to the dish.
