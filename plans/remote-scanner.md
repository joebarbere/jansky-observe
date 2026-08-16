# Remote RFI scanner — concurrent two-source observing

*Plan written 2026-08-15. Trigger: the operator wants to scan for RFI with a HackRF on a
MacBook while the Pi + Airspy + dish observes HI, over the LAN (Ethernet), with automatic
connection and visible status.*

## The problem

The station's RFI sweep (`POST /api/rfi_sweep`, v0.7) is a *blocking daemon command*: the
HackRF must hang off the Pi, and the live HI frame stream pauses for the sweep's duration.
Surveying and observing are mutually exclusive by construction.

## The design

Move the HackRF to a second host and make the server accept sweeps over HTTP. Three parts,
no schema change (`user_version` stays 14 — remote metadata rides in the existing
`Capture.sdr_settings` JSON), no `install.sh`/`OS_IMAGE` change ⇒ no QEMU gate:

1. **Ingest** (`server/routers/remote_scan.py`):
   - `POST /api/remote_scan/sweep` — body is the raw `hackrf_sweep` CSV (the same "the CSV
     *is* the capture" convention as the local path); query params carry `host`, `antenna`,
     `label`, `num_sweeps`. The server validates via `hackrf_sweep.averaged_bins`, writes
     `captures/rfi-<utcstamp>Z-remote.csv`, and registers a `Capture` (device
     `hackrf-remote`, format `hackrf_sweep_csv`) — so remote sweeps appear in the existing
     RFI view (occupancy and all) with zero daemon involvement. An upload doubles as a
     heartbeat.
   - `POST /api/remote_scan/heartbeat` + `GET /api/remote_scan/status` — an in-memory
     last-seen registry on `app.state` (a scanner is *connected* when its last heartbeat is
     < 90 s old). In-memory is deliberate: presence is ephemeral; restarts should forget it.
2. **Status** — a `scanner` chip in the status-bar payload (`status_bar.py`) and cockpit bar:
   `scanner: macbook · 12 s` (green when connected, grey `scanner: —` otherwise). This is
   the operator-visible "the two computers see each other".
3. **Discovery** (`server/discovery.py` + `deploy/remote_scan.py`):
   - The server advertises `_jansky-observe._tcp.local.` via python-zeroconf (new
     dependency), TXT carrying the API path and version. Import- and failure-guarded:
     no zeroconf, no network, or `JANSKY_OBSERVE_NO_MDNS=1` ⇒ the server runs exactly as
     before. This is the scoped-down first slice of the parked "v2 multi-station mDNS"
     roadmap item — advertisement only, no peering.
   - The client (`deploy/remote_scan.py`, **stdlib-only**, runs on macOS/Linux with no
     install) resolves the station in order: `--station` → `$JANSKY_STATION_URL` → mDNS
     browse (`dns-sd` on macOS, `avahi-browse` on Linux, both optional) →
     `http://raspberrypi.local:8000`. Then it loops: heartbeat every 15 s, `hackrf_sweep`
     every `--interval` minutes, POST the CSV. `--once` for a single sweep; `--dry-run`
     for wiring checks without a HackRF.

## Safety

The bias-tee rule is untouched: the client shells out through the same no-`-p`-flag
`build_sweep_cmd` contract (it constructs its own command but hard-excludes port power the
same way — a guard test mirrors `test_profiles.py`'s).

## Tests

Synthetic-fixture only: upload/heartbeat/status via `TestClient`; the status-bar chip; the
discovery module against a fake `zeroconf` injected into `sys.modules`; the client's
resolution order and command construction (imported as a module — it lives in `deploy/` but
is importable), including the no-port-power guard.
