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

## Sharing with the siblings

Skills are discovered only from *this* repo's `.claude/skills/` — anything shared with `jansky`
or `jansky-research` is **copied, not symlinked** (same policy as
`../jansky-research/.claude/README.md`). If a shared skill changes meaningfully in one repo,
mirror it in the others. The three skills above are station-specific and stay here.

## Roadmap (plan §12.6)

More assets ship with their milestones — the loop exists before the features do:

- **M2** — the MCP surface mounts on the API server (Claude becomes a console peer of the
  browser UI), plus `/plan-session`, `/troubleshoot-chain`, `/new-migration`.
- **M3** — `/observing-copilot`, `/analyze-observation`, and the `hi-data-analyst` +
  `dsp-reviewer` agents.
- **M4** — `/write-up`, `/compare-observations`.
