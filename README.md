# jansky-observe

[![CI](https://github.com/joebarbere/jansky-observe/actions/workflows/ci.yml/badge.svg)](https://github.com/joebarbere/jansky-observe/actions/workflows/ci.yml)

**Observation management for the Discovery Dish station — plan, run, and record attended radio
observations end to end.**

Station software for a hydrogen-line telescope: KrakenRF Discovery Dish 700 mm + H-line feed
(1420 MHz) → Airspy Mini → Raspberry Pi 5, in Manayunk, Philadelphia. Two tiers: a Python server
side (a FastAPI API server plus a separate SDR-owning capture daemon, so a USB hiccup never takes
down the observation record) and a thin browser UI that works from any laptop or tablet on the
LAN. Sibling of the [`jansky`](https://github.com/joebarbere/jansky) course (whose tested library
this reuses) and [`jansky-research`](https://github.com/joebarbere/jansky-research).

```mermaid
flowchart LR
    subgraph pi["Raspberry Pi 5"]
        cap["capture daemon<br/>SDR + FFT frames"]
        api["FastAPI server<br/>REST + WebSocket + UI"]
        files[("data/ on disk<br/>SigMF IQ · spectra · reports")]
        cap -- "ZMQ frames" --> api
        cap --> files
        api --> files
    end
    airspy["Airspy Mini ← H-line feed ← 700 mm dish"] --> cap
    browser["Browser UI (laptop / tablet)"] <-- "HTTP + WebSocket" --> api
```

## Status

**M0 — walking skeleton — shipped:** synthetic capture daemon → ZMQ → WebSocket → canvas
waterfall, plus the full pipeline (CI, release workflow with a release-blocking install gate,
`install.sh`, QEMU install test against the pinned Pi OS image).

| Tag | Milestone | Release means | |
|---|---|---|---|
| `v0.1.0` | M0 | Walking skeleton + the whole CI/release/install pipeline | ✅ done |
| `v0.2.0` | M1 | First light: real Airspy, live view, captures to disk | ⏭ next |
| `v0.3.0` | M2 | Observation records, checklists, session wizard | |
| `v0.4.0` | M3 | Confirmation: rule-based classifier + HI4PI cross-check | |
| `v0.5.0` | M4 | Reports & photos: PDF export, Virgo/ezRA exporters | |
| `v0.6.0` | M5 | Feature-complete — the `v1.0.0` release candidate | |
| `v1.0.0` | — | After one real end-to-end observing campaign | |

## Quickstart (dev)

Requires the `jansky` repo checked out next to this one (`../jansky`).

```bash
git clone https://github.com/joebarbere/jansky-observe.git   # next to ../jansky
cd jansky-observe
make setup          # uv sync — pinned Python 3.12 env

# Two terminals:
make daemon         # capture daemon, synthetic noise + fake-HI source
make run            # API server at http://localhost:8000

# Open http://localhost:8000 — live waterfall from the synthetic source.
```

`make help` lists the rest (`test`, `cov`, `lint`, `typecheck`, `qemu-install`, …).

## Install on the Pi

One prerequisite: a Raspberry Pi 5 running the pinned **Raspberry Pi OS Lite (64-bit) "Trixie"**
image (exact image in [`deploy/OS_IMAGE`](deploy/OS_IMAGE)), flashed with SSH enabled. Everything
else is the install script's job:

```bash
curl -fsSL https://github.com/joebarbere/jansky-observe/releases/latest/download/install.sh | sudo bash
```

Idempotent and re-runnable (re-running upgrades in place); installs apt deps, uv, the release
wheel, udev rules, and the two systemd units, then health-checks itself. Flags: `--version vX.Y.Z`,
`--no-start`, `--uninstall`.

## Layout

```
jansky-observe/
  src/jansky_observe/
    capture/      # SDR-owning daemon: sources, Welch PSD, device profiles, ZMQ publisher
    server/       # FastAPI app: REST, WebSocket live view, templates, waterfall.js
    frames.py     # spectral-frame wire formats (daemon → server → browser)
    synthetic.py  # synthetic noise + fake-HI generators (M0, test fixtures)
    config.py     # JANSKY_OBSERVE_* env settings
  tests/          # pytest, synthetic fixtures only — no hardware in CI
  deploy/         # install.sh, OS_IMAGE, systemd units, udev rules, QEMU install test
  plans/          # the full project plan
  .claude/        # versioned Claude Code skills (see .claude/README.md)
```

## Plan

The full project plan — architecture, data model, integrations, milestones — lives in
[`plans/jansky_observe.md`](plans/jansky_observe.md).

## Siblings

- [`jansky`](https://github.com/joebarbere/jansky) — the radio-astronomy course; this repo
  depends on its library (`jansky.signals`, `jansky.formats`, …).
- [`jansky-research`](https://github.com/joebarbere/jansky-research) — the research repo; once
  the station produces calibrated spectra, self-collected data feeds its slices.

## License

MIT — see [LICENSE](LICENSE).
