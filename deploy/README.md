# deploy/ — the delivery pipeline

Install is a release artifact, not a doc page (plan §9). Everything here exists so that
every milestone tag becomes an installable GitHub Release, gated on "does it actually
install on a clean Pi OS?".

## Prerequisites (the only manual setup — plan §9)

- A **Raspberry Pi 5** running **Raspberry Pi OS Lite (64-bit), Debian 13 "Trixie"** —
  the exact supported image is pinned in [`OS_IMAGE`](OS_IMAGE). Flash it with Raspberry
  Pi Imager with SSH enabled.
- Network + SSH access to the Pi.
- **Everything else is `install.sh`'s job.** If a setup step can't be scripted, it goes
  in this list or it doesn't exist.

Then, on the Pi:

```sh
curl -fsSL https://github.com/joebarbere/jansky-observe/releases/latest/download/install.sh | sudo bash
```

Re-running upgrades in place. `sudo bash install.sh --uninstall` removes the services and
venv but keeps the observation data in `/var/lib/jansky-observe`. See
`bash install.sh --help` for all flags (`--version`, `--wheel`, `--jansky-ref`,
`--no-start`, `--smoke`, `--allow-unsupported-os`).

## What each piece is

| Path | What it is |
|---|---|
| `install.sh` | The one idempotent installer, shipped as a release asset: OS check, apt deps (airspy/hackrf userland, libusb), pinned uv, `/opt/jansky-observe/venv` (uv-managed Python 3.12), `jansky` from its git tag + the release wheel, udev rules, data dir + `jansky` system user, the two systemd units, health check. In a container (or with `--smoke`) it runs both processes in the foreground and reads one live WebSocket frame instead of using systemd. |
| `OS_IMAGE` | The pinned Raspberry Pi OS Lite 64-bit image (name/date/URL/SHA-256) — the one manual assumption. Changing the pin requires re-running `make qemu-install` before the next tag. |
| `systemd/jansky-observe.service` | API server unit (`jansky-observe --host 0.0.0.0 --port 8000`, user `jansky`). |
| `systemd/jansky-observe-capture.service` | Capture daemon unit (`jansky-observe-capture --synthetic` at M0; M1 swaps the flag for the real Airspy). |
| `udev/99-jansky-observe-sdr.rules` | Airspy Mini (1d50:60a1) + HackRF One (1d50:6089): mode 0660, group `plugdev`, `TAG+="uaccess"`. |
| `qemu/run-install-test.sh` | The full-fidelity install gate (`make qemu-install`), see below. |

## The drift-check contract

`install.sh` must be standalone (`curl | sudo bash`), so the systemd units and udev rules
are **embedded in it** as heredocs. The files under `systemd/` and `udev/` are the same
bytes — they exist so the units are reviewable/diffable as files. CI enforces
byte-equality on every push:

```sh
bash deploy/install.sh --print-unit server  | diff -u deploy/systemd/jansky-observe.service -
bash deploy/install.sh --print-unit capture | diff -u deploy/systemd/jansky-observe-capture.service -
bash deploy/install.sh --print-udev         | diff -u deploy/udev/99-jansky-observe-sdr.rules -
```

To change a unit or the udev rules: edit the heredoc in `install.sh`, then regenerate the
deploy copy with the matching `--print-*` command (as above, `>` instead of `diff`).

## The QEMU install gate (`make qemu-install`)

Boots the **pinned genuine Raspberry Pi OS image** headless in `qemu-system-aarch64`
(`-M virt -cpu cortex-a76`, kernel/initramfs extracted from the image, first-boot user
pre-seeded, SSH port-forwarded to `localhost:5022`), runs the real `install.sh` in the
guest — the true systemd path, unlike the container gate — and asserts `/healthz` +
`--version`. If a wheel exists in `dist/` (`make build`), it gates that wheel; otherwise
it installs the latest published release.

- **When it's required (release-blocking, plan §9):** before tagging `v0.1.0`, and
  whenever `install.sh` or the `OS_IMAGE` pin changes. Optional otherwise — too slow for
  every push. The `/release` skill checks this.
- Host tools (Fedora): `sudo dnf install qemu-system-aarch64 qemu-img guestfs-tools
  sshpass openssl xz`. Without guestfish it falls back to `sudo` loop mounts.
- The image download is cached in `qemu/cache/` (gitignored); a warm run is minutes.
- **Honest limits:** QEMU emulates the OS and userland, not Pi 5 silicon or USB. SDR
  enumeration (`airspy_rx` actually seeing the device) stays a physical checklist item on
  the real Pi.

## CI / release flow (`.github/workflows/`)

- `ci.yml` — every push/PR, on `ubuntu-latest` **and** `ubuntu-24.04-arm` (the Pi's
  architecture): ruff, mypy, pytest (+85% coverage floor), wheel build, the drift check,
  `bash -n` on the shell scripts.
- `release.yml` — on tag `v*`: full checks → build → **install gate** (pristine
  `debian:trixie` arm64 container runs `install.sh --wheel … --smoke`) → GitHub Release
  with the wheel/sdist, `install.sh`, and `SHA256SUMS`. **A tag whose gate fails
  publishes nothing** — the job ordering guarantees it.
