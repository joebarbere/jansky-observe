# Claude Code automation for jansky-observe

This directory holds the project's versioned **skills** and **agents**. See the repo `CLAUDE.md`
for the working rules, the **safety invariants** (the bias-tee rule), and the relationship to the
siblings (`../jansky`, `../jansky-research`).

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
- `observing-copilot` — the during-session companion: polls `get_live_status` + the live HI
  badge (running SNR on the accumulating average) and reports the trend, warns when pointing
  drift approaches half the 21° beam (nudge at ~10°, minutes-to-nudge from the drift rate),
  answers "RFI or the line?" (Doppler window + persistence across frames), and appends
  timestamped notes on request. Interprets the badge; never overrides it.
- `analyze-observation` — post-session, for one observation id: run the deterministic
  `hline_v1` classifier over every `.npz` capture via `run_classifier`, fetch `axis=vlsr`
  spectra, interpret baseline quality / SNR / peak-velocity plausibility / RFI signs, and
  write a markdown analysis note whose claims cite `ClassifierResult` rows — no unsupported
  detection language (plan §12.5). Notes that v1 verdicts are threshold evidence only until
  `hi4pi_xcheck` lands via jansky-research plan 78.
- `write-up` — draft the report narrative in house honesty standards: gather the observation
  + classifier results (running `hline_v1` first on anything unclassified), write the plan-§7
  sections (attempted / conditions / recorded / classifier found / interpretation — clearly
  labeled / next steps), `append_note` the draft so the report renders it, then
  `build_report` for the PDF at `data/observations/<id>/report.pdf`. Hard rules: claims cite
  `ClassifierResult` rows, and a `detected` peak at an implausible galactic v_LSR is written
  up as probable RFI (the 1421.25 MHz bare-input spur precedent).
- `compare-observations` — cross-session, same source over days/weeks: pull `axis=vlsr`
  spectra + classifier results per done observation, then four checks — stacking (≈ √N
  gain), SNR vs integration time (√t law; deviations implicate gain drift/RFI),
  peak-v_LSR stability (secular drift = frequency-calibration alarm; the LSR axis should have
  removed the ±30 km/s annual term), pointing repeatability (growing dialed-vs-computed
  offsets → redo Sun cal). Output: a markdown comparison table + findings, appended to the
  newest observation.

## Agents (`agents/`)

- `hi-data-analyst` — the amateur-radio-astronomer-analyst persona behind the post-session
  skills: station numbers (0.7 m dish, HPBW ≈ 21°, 3 MSPS ≈ ±250 km/s, Manayunk), 21 cm
  physics and v_LSR conventions, the v1 classifier's exact thresholds and limits, the
  provenance rule, and the wrong-spectrum suspect order (RFI → baseline ripple →
  frequency cal → pointing).
- `dsp-reviewer` — read-only reviewer for diffs touching `capture/dsp.py`, `astro/`, or
  `confirm/`: checks against the plan's numbers and astropy conventions and hunts the classic
  unit bugs (topocentric vs LSR axis, Hz vs km/s, 10 vs 20·log10, fftshift, int16 `/32768`
  scaling, Welch normalization, radio vs optical Doppler). Returns findings; makes no edits.

## Sharing with the siblings

Skills are discovered only from *this* repo's `.claude/skills/` — anything shared with `jansky`
or `jansky-research` is **copied, not symlinked** (same policy as
`../jansky-research/.claude/README.md`). If a shared skill changes meaningfully in one repo,
mirror it in the others. The skills above are station-specific and stay here.

## Roadmap (plan §12.6)

Assets ship with their milestones — the loop exists before the features do. With M4's
`/write-up` and `/compare-observations`, **every asset in plan §12.6 has shipped**: M0's
developer loop, M2's MCP surface + observer skills, M3's analyst skills and agents, M4's
report/comparison skills. M5 (polish) has no Claude deliverables — changes here from now on
are maintenance, mirrored to the siblings where shared.
