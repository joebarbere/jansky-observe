# Claude Code automation for jansky-observe

This directory holds the project's versioned **skills**. See the repo `CLAUDE.md` for the working
rules, the **safety invariants** (the bias-tee rule), and the relationship to the siblings
(`../jansky`, `../jansky-research`).

## Skills (`skills/`)

- `verify` — the pre-commit gate: lint → typecheck → coverage (85% floor) → the end-to-end
  synthetic smoke (daemon → ZMQ → server → WebSocket frame).
- `release` — the milestone-close procedure from plan §9: QEMU gate when `deploy/install.sh` /
  `deploy/OS_IMAGE` changed, `/verify`, tag, watch the release workflow's install gate, upgrade
  the real Pi.
- `synthetic-fixture` — generate deterministic synthetic IQ/spectrum fixtures (noise + fake-HI
  line, RFI spikes, ripple, v_LSR offsets) so DSP/classifier tests never need hardware or sky.
- `plan-session` — "what should I point at tonight?": `whats_up` + `get_weather` via the
  station MCP, filtered to the 0–70° elevation limit and ranked by the observing ladder
  (plan §5.4), transit timing, beam-crossing, and the weather window; ends with a pre-filled
  draft Observation for the wizard.
- `troubleshoot-chain` — the no-signal decision tree in strict order: injector current
  (~120 mA) → bias-tee states (*checked, never changed*) → gain ladder → USB enumeration →
  daemon status → frequency sanity → tinySA bench fallback; findings appended to the
  observation, because failed sessions are data too.
- `new-migration` — scaffold a forward schema migration: the next `(N, callable)` in
  `db.py`'s `MIGRATIONS` (`PRAGMA user_version`), the matching `models.py` change, and the
  round-trip test. Never edit or renumber an existing migration.

## Sharing with the siblings

Skills are discovered only from *this* repo's `.claude/skills/` — anything shared with `jansky`
or `jansky-research` is **copied, not symlinked** (same policy as
`../jansky-research/.claude/README.md`). If a shared skill changes meaningfully in one repo,
mirror it in the others. The skills above are station-specific and stay here.

## Roadmap (plan §12.6)

More assets ship with their milestones — the loop exists before the features do. M2's
deliverables (the MCP surface on the API server — Claude as a console peer of the browser
UI — plus `/plan-session`, `/troubleshoot-chain`, `/new-migration`) have shipped; remaining:

- **M3** — `/observing-copilot`, `/analyze-observation`, and the `hi-data-analyst` +
  `dsp-reviewer` agents.
- **M4** — `/write-up`, `/compare-observations`.
