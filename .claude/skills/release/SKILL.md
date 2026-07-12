---
name: release
description: Close a jansky-observe milestone as a release — preconditions, the QEMU install gate when deploy/ changed, /verify, tag, watch the release workflow's install gate, then upgrade the real Pi. Use when tagging any vX.Y.Z or when the user says "cut a release" / "close the milestone".
---

# Release: the milestone-close procedure (plan §9)

Every milestone closes the same way. Execute the steps in order; a failure at any step stops the
release. Semver is pre-1.0: **minor = milestone, patch = fixes between milestones.**

| Tag | Milestone | Release means |
|---|---|---|
| `v0.1.0` | M0 | Walking skeleton + the whole CI/release/install pipeline |
| `v0.2.0` | M1 | First light: real Airspy, live view, captures to disk |
| `v0.3.0` | M2 | Observation records, checklists, session wizard |
| `v0.4.0` | M3 | Confirmation: v1 classifier + HI4PI cross-check |
| `v0.5.0` | M4 | Reports & photos: PDF export, exporters |
| `v0.6.0` | M5 | Feature-complete — the `v1.0.0` release candidate |
| `v1.0.0` | — | After one real end-to-end observing campaign on a `v0.6.x` install |

## 0. Preconditions

- On `main`, clean tree (`git status`), up to date with origin.
- Version bumped in **both** `pyproject.toml` and `src/jansky_observe/__init__.py`
  (`__version__`) — they must match the tag.
- Release notes gathered: skim `git log <last-tag>..HEAD` for the CHANGELOG-worthy items
  (the workflow auto-generates notes; make sure the log tells the story).

## 1. QEMU gate — required if the install path changed

```bash
git diff $(git describe --tags --abbrev=0) -- deploy/install.sh deploy/OS_IMAGE
```

If that diff is non-empty, `make qemu-install` MUST pass before tagging — it runs the real
`install.sh` inside the pinned genuine Raspberry Pi OS image under QEMU. This is
release-blocking (plan §9); do not tag around it.

## 2. Run /verify

Lint → typecheck → coverage → the end-to-end synthetic smoke. All green or no tag.

## 3. Tag and push

```bash
git tag vX.Y.Z
git push origin vX.Y.Z
```

## 4. Watch the release workflow

```bash
gh run watch --repo joebarbere/jansky-observe
```

`release.yml` runs the full CI matrix, builds the wheel, then the **install gate** (pristine
Trixie arm64 container runs `install.sh` against the just-built artifacts, asserts services +
healthz + a synthetic capture smoke), and only then publishes the Release. Confirm:

- the install gate job passed;
- the Release exists with **wheel + `install.sh` + `SHA256SUMS`** attached.

## 5. Upgrade the real Pi

```bash
curl -fsSL https://github.com/joebarbere/jansky-observe/releases/latest/download/install.sh | sudo bash
# or pin: ... | sudo bash -s -- --version vX.Y.Z
```

Then on the Pi: `curl -fsS localhost:8000/healthz` and `jansky-observe --version` shows the new
version. The milestone is closed only when the real station runs it.

## 6. Flip the docs to shipped

The docs are written *during* the milestone, so they say "in progress" / "⏭ current" until
someone flips them. That someone is this step — do it right after the Pi upgrade, as a small
docs PR:

- README.md milestone table: this release's row → `✅ done`; the next row → `⏭ next`.
- CLAUDE.md `## Current status`: past tense for this milestone, name the next one.

A release isn't finished while the README calls it current.

## 7. If the install gate fails

Fix the problem, **delete nothing** (no tag deletion, no force-push), bump the patch version,
and re-tag `vX.Y.(Z+1)`. A tag whose gate failed publishes nothing by design — it's inert, not
an emergency.
