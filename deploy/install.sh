#!/usr/bin/env bash
# deploy/install.sh — jansky-observe installer / upgrader / uninstaller.
#
# Shipped as a GitHub Release asset (plan §9). One idempotent script:
#
#   curl -fsSL https://github.com/joebarbere/jansky-observe/releases/latest/download/install.sh | sudo bash
#
# or download-inspect-run:
#
#   sudo bash install.sh [--version vX.Y.Z] [--wheel path.whl] [--jansky-ref vX.Y.Z]
#                        [--no-start] [--smoke] [--uninstall] [--reset-data [--yes]]
#                        [--install-argon [--argon-nvme-boot]] [--allow-unsupported-os]
#
# Supported base: Raspberry Pi OS Lite 64-bit, Debian 13 "Trixie", aarch64 — the ONE manual
# prerequisite (plan §9). The exact supported image is pinned in deploy/OS_IMAGE. Anything else
# is refused rather than half-installed, unless --allow-unsupported-os.
#
# Re-running is an in-place upgrade. --uninstall removes the units/venv/udev rules but keeps
# the observation data in /var/lib/jansky-observe. --reset-data is the opposite: it keeps the
# install but wipes the observation data (QA / clean install — see README).
#
# The systemd units and udev rules are EMBEDDED below as heredocs so this script is standalone.
# The copies in deploy/systemd/ and deploy/udev/ are the same bytes; CI enforces that with:
#   install.sh --print-unit server|capture   and   install.sh --print-udev
# diffed against the deploy/ files (the drift check — see .github/workflows/ci.yml).

set -euo pipefail

# ---------------------------------------------------------------------------
# Pins & paths
# ---------------------------------------------------------------------------

UV_VERSION="0.11.28"          # pinned uv (installed to /usr/local/bin if absent)
PYTHON_VERSION="3.12"         # uv-managed interpreter for the venv (Trixie's system python is 3.13)
DEFAULT_JANSKY_REF="v0.2.0"   # jansky is NOT on PyPI; installed from its git tag first

REPO="joebarbere/jansky-observe"
JANSKY_GIT_URL="https://github.com/joebarbere/jansky"

PREFIX="/opt/jansky-observe"
VENV="${PREFIX}/venv"
DATA_DIR="/var/lib/jansky-observe"
SERVICE_USER="jansky"
UNIT_DIR="/etc/systemd/system"
UDEV_RULES_FILE="/etc/udev/rules.d/99-jansky-observe-sdr.rules"
DEFAULT_FILE="/etc/default/jansky-observe"
HEALTH_URL="http://127.0.0.1:8000/healthz"
HEALTH_TIMEOUT=90             # seconds to wait for /healthz

# Argon ONE V5 M.2 NVMe case (optional, --install-argon). The Pi 5 lives in this case; the
# NVMe on its M.2 slot is just the Pi 5 PCIe bus, which we enable in config.txt. The fan +
# power-button "drivers" are Argon40's own daemon (it speaks to the case's MCU), so that half
# is their official installer. NVMe *boot* — as opposed to seeing the NVMe as a data disk —
# is the extra boot-critical EEPROM step, gated behind --argon-nvme-boot.
ARGON_INSTALLER_URL="https://download.argon40.com/argon1v5.sh"
BOOT_CONFIG_CANDIDATES=(/boot/firmware/config.txt /boot/config.txt)  # Trixie firmware path first

# libpango*/fonts: WeasyPrint's system dependencies (PDF reports, M4).
# sqlite3: the CLI for hand-inspecting /var/lib/jansky-observe/*.sqlite3 when
# debugging on the Pi (the app uses Python's sqlite3, but Pi OS Lite ships no CLI).
APT_DEPS=(curl ca-certificates git libusb-1.0-0 airspy hackrf sqlite3
    libpango-1.0-0 libpangoft2-1.0-0 fonts-dejavu-core)

# ---------------------------------------------------------------------------
# Embedded deploy assets (source of truth at install time).
# CI diffs these against deploy/systemd/ and deploy/udev/ so they can never drift.
# ---------------------------------------------------------------------------

print_unit_server() {
    cat <<'UNIT_EOF'
# jansky-observe API server — REST + WebSocket fan-out + browser UI (plan §2).
# Installed by deploy/install.sh, which embeds an identical copy; CI diffs the two
# (install.sh --print-unit server) so this file and the installer can never drift.
[Unit]
Description=jansky-observe API server (REST + WebSocket + UI)
Documentation=https://github.com/joebarbere/jansky-observe
Wants=network-online.target
After=network-online.target

[Service]
Type=exec
User=jansky
Group=jansky
# plugdev matches the udev rules (deploy/udev/). The server itself never touches the
# SDRs, but parity with the capture daemon keeps permissions unsurprising.
SupplementaryGroups=plugdev
Environment=JANSKY_OBSERVE_ZMQ_ENDPOINT=tcp://127.0.0.1:8410
Environment=JANSKY_OBSERVE_CTL_ENDPOINT=tcp://127.0.0.1:8411
Environment=JANSKY_OBSERVE_DATA_DIR=/var/lib/jansky-observe
WorkingDirectory=/var/lib/jansky-observe
ExecStart=/opt/jansky-observe/venv/bin/jansky-observe --host 0.0.0.0 --port 8000
Restart=on-failure
RestartSec=2

# Hardening — deliberately stops short of PrivateDevices/DeviceAllow, which would
# break USB SDR access for the real Airspy source (--source airspy).
NoNewPrivileges=true
ProtectSystem=full
ProtectHome=true
PrivateTmp=true
ReadWritePaths=/var/lib/jansky-observe

[Install]
WantedBy=multi-user.target
UNIT_EOF
}

print_unit_capture() {
    cat <<'UNIT_EOF'
# jansky-observe capture daemon — owns the SDR hardware, streams spectral frames to the
# API server over ZMQ (plan §2). A USB hiccup here must never take down the API server,
# which is why this is a separate unit.
# Installed by deploy/install.sh, which embeds an identical copy; CI diffs the two
# (install.sh --print-unit capture) so this file and the installer can never drift.
[Unit]
Description=jansky-observe capture daemon (SDR owner; source set in /etc/default/jansky-observe)
Documentation=https://github.com/joebarbere/jansky-observe
Wants=network-online.target
After=network-online.target

[Service]
Type=exec
User=jansky
Group=jansky
# plugdev matches the udev rules (deploy/udev/) — this is what grants USB SDR access.
SupplementaryGroups=plugdev
Environment=JANSKY_OBSERVE_ZMQ_ENDPOINT=tcp://127.0.0.1:8410
Environment=JANSKY_OBSERVE_CTL_ENDPOINT=tcp://127.0.0.1:8411
Environment=JANSKY_OBSERVE_DATA_DIR=/var/lib/jansky-observe
# Source selection (M1, plan §10): synthetic is the safe installed default; the operator
# switches to the real Airspy in /etc/default/jansky-observe (written by install.sh only
# if absent), never by editing this unit. The EnvironmentFile line below overrides the
# default above.
Environment=JANSKY_OBSERVE_SOURCE=synthetic
# Frame rate (roadmap M6): each row is an integrated spectrum, so this is a
# spectrometer cadence, not a render limit — raising it trades integration per
# row for smoother motion. Overridable in /etc/default/jansky-observe.
Environment=JANSKY_OBSERVE_FPS=4.0
EnvironmentFile=-/etc/default/jansky-observe
WorkingDirectory=/var/lib/jansky-observe
ExecStart=/opt/jansky-observe/venv/bin/jansky-observe-capture --source ${JANSKY_OBSERVE_SOURCE} --fps ${JANSKY_OBSERVE_FPS}
Restart=on-failure
RestartSec=2

# Hardening — deliberately stops short of PrivateDevices/DeviceAllow, which would
# break USB SDR access for the real Airspy source (--source airspy).
NoNewPrivileges=true
ProtectSystem=full
ProtectHome=true
PrivateTmp=true
ReadWritePaths=/var/lib/jansky-observe

[Install]
WantedBy=multi-user.target
UNIT_EOF
}

print_udev() {
    cat <<'UDEV_EOF'
# jansky-observe device udev rules — make the station's receivers and the rotator
# usable without root: GROUP=plugdev covers the jansky service user, TAG+="uaccess"
# covers a physically seated user for bench debugging.
# Installed by deploy/install.sh, which embeds an identical copy; CI diffs the two
# (install.sh --print-udev) so this file and the installer can never drift.

# Airspy Mini (primary receiver, H-line feed)
SUBSYSTEM=="usb", ATTRS{idVendor}=="1d50", ATTRS{idProduct}=="60a1", MODE="0660", GROUP="plugdev", TAG+="uaccess"

# HackRF One (RFI survey / injection test)
SUBSYSTEM=="usb", ATTRS{idVendor}=="1d50", ATTRS{idProduct}=="6089", MODE="0660", GROUP="plugdev", TAG+="uaccess"

# Discovery Drive rotator over USB-serial (roadmap M9, EasyComm II). The ESP32-S3
# enumerates via one of a few USB-serial bridges; a stable /dev/jansky-rotator
# symlink lets the station config name the port regardless. rotctl-TCP needs no rule.
SUBSYSTEM=="tty", SUBSYSTEMS=="usb", ATTRS{idVendor}=="10c4", ATTRS{idProduct}=="ea60", MODE="0660", GROUP="plugdev", SYMLINK+="jansky-rotator", TAG+="uaccess"
SUBSYSTEM=="tty", SUBSYSTEMS=="usb", ATTRS{idVendor}=="1a86", ATTRS{idProduct}=="7523", MODE="0660", GROUP="plugdev", SYMLINK+="jansky-rotator", TAG+="uaccess"
SUBSYSTEM=="tty", SUBSYSTEMS=="usb", ATTRS{idVendor}=="303a", MODE="0660", GROUP="plugdev", SYMLINK+="jansky-rotator", TAG+="uaccess"
UDEV_EOF
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

log() { printf '\033[1;34m[install]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[install]\033[0m %s\n' "$*" >&2; }
die() { printf '\033[1;31m[install]\033[0m ERROR: %s\n' "$*" >&2; exit 1; }

usage() {
    cat <<'USAGE_EOF'
jansky-observe installer (see deploy/README.md)

Usage: sudo bash install.sh [flags]

  --version vX.Y.Z        Release to install (default: latest GitHub release)
  --wheel <path>          Install a local wheel instead of downloading (CI install gate)
  --jansky-ref <tag>      Git ref of the jansky library dependency (default: v0.2.0)
  --no-start              Install and enable, but do not start the services
  --smoke                 Container/CI mode: skip systemd, run both processes in the
                          foreground, poll /healthz, read one live WebSocket frame
  --uninstall             Remove units/venv/udev rules; keep /var/lib/jansky-observe
  --reset-data            QA / clean install: stop the services, delete EVERYTHING in
                          /var/lib/jansky-observe (observations, captures, photos,
                          reports), restart fresh (migrations + seeds rebuild the DB)
                          and exit. Asks for confirmation on a TTY; needs --yes without
                          one (e.g. curl | sudo bash -s -- --reset-data --yes)
  --yes                   Skip the --reset-data confirmation prompt
  --set-source synthetic|airspy
                          Switch the capture source (writes /etc/default/jansky-observe,
                          restarts the capture service) and exit — no reinstall
  --install-argon         Set up the Argon ONE V5 M.2 NVMe case and exit: enable the Pi 5
                          PCIe/M.2 slot in config.txt + install Argon's fan/power-button
                          daemon. Idempotent; reboot afterwards. Pi 5 only.
  --argon-nvme-boot       With --install-argon, also update the bootloader EEPROM to an
                          NVMe-first boot order (boot-critical; needs --yes on a non-TTY)
  --allow-unsupported-os  Skip the OS/architecture check (at your own risk)
  --print-unit server|capture   Print an embedded systemd unit (drift check; no root)
  --print-udev            Print the embedded udev rules (drift check; no root)
  -h, --help              This help
USAGE_EOF
}

have() { command -v "$1" >/dev/null 2>&1; }

# systemd is managing this machine iff /run/systemd/system exists (the documented check;
# true on a real Pi, false inside the release-gate container).
systemd_running() { [[ -d /run/systemd/system ]]; }

# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------

check_os() {
    # Plan §9: refuse unsupported bases rather than half-installing. The one supported
    # base is the pinned Raspberry Pi OS Lite 64-bit (Trixie) image in deploy/OS_IMAGE;
    # any Debian/Raspbian Trixie on aarch64 passes (the release gate runs debian:trixie).
    local os_id os_codename arch
    [[ -r /etc/os-release ]] || die "cannot read /etc/os-release (use --allow-unsupported-os to override)"
    os_id="$(. /etc/os-release && echo "${ID:-} ${ID_LIKE:-}")"
    os_codename="$(. /etc/os-release && echo "${VERSION_CODENAME:-}")"
    arch="$(uname -m)"
    case " ${os_id} " in
        *debian* | *raspbian*) : ;;
        *) die "unsupported OS '${os_id}' — supported base is Raspberry Pi OS Lite 64-bit (Trixie), see deploy/OS_IMAGE (--allow-unsupported-os to override)" ;;
    esac
    [[ "${os_codename}" == "trixie" ]] \
        || die "unsupported release '${os_codename}' — need Debian 13 'trixie' (--allow-unsupported-os to override)"
    [[ "${arch}" == "aarch64" ]] \
        || die "unsupported architecture '${arch}' — need aarch64 (--allow-unsupported-os to override)"
    log "OS check passed: ${os_id% } / ${os_codename} / ${arch}"
}

apt_install() {
    if ! have apt-get; then
        warn "apt-get not found — skipping system package installation"
        return 0
    fi
    log "installing apt dependencies: ${APT_DEPS[*]}"
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y -qq --no-install-recommends "${APT_DEPS[@]}"
}

ensure_uv() {
    # Pinned uv into /usr/local/bin if absent. A pre-existing uv of a different version
    # is used as-is (never clobber the operator's tooling) but warned about.
    local uv_bin="/usr/local/bin/uv" current
    if have uv; then
        UV="$(command -v uv)"
        current="$("${UV}" --version | awk '{print $2}')"
        if [[ "${current}" != "${UV_VERSION}" ]]; then
            warn "found uv ${current} at ${UV} (pinned: ${UV_VERSION}) — using it anyway"
        else
            log "uv ${UV_VERSION} already installed"
        fi
        return 0
    fi
    log "installing uv ${UV_VERSION} to /usr/local/bin"
    curl -fsSL "https://astral.sh/uv/${UV_VERSION}/install.sh" \
        | env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 INSTALLER_NO_MODIFY_PATH=1 sh
    UV="${uv_bin}"
    "${UV}" --version >/dev/null || die "uv installation failed"
}

ensure_user_and_dirs() {
    if ! id -u "${SERVICE_USER}" >/dev/null 2>&1; then
        log "creating system user '${SERVICE_USER}'"
        useradd --system --user-group --home-dir "${DATA_DIR}" \
            --shell /usr/sbin/nologin "${SERVICE_USER}"
    fi
    getent group plugdev >/dev/null || groupadd --system plugdev
    usermod -aG plugdev "${SERVICE_USER}"
    install -d -m 0755 "${PREFIX}"
    # Data dir: owned by the service user; survives upgrades AND --uninstall.
    install -d -m 0755 -o "${SERVICE_USER}" -g "${SERVICE_USER}" "${DATA_DIR}"
}

ensure_venv() {
    # uv-managed Python (Trixie ships 3.13; the project pins 3.12) kept under /opt so
    # nothing leaks into root's home. Reused on re-run; recreated only if broken.
    export UV_PYTHON_INSTALL_DIR="${PREFIX}/python"
    if [[ -x "${VENV}/bin/python" ]] && "${VENV}/bin/python" -c 'pass' 2>/dev/null; then
        log "reusing existing venv at ${VENV}"
        return 0
    fi
    log "creating venv at ${VENV} (uv-managed Python ${PYTHON_VERSION})"
    rm -rf "${VENV}"
    "${UV}" python install "${PYTHON_VERSION}"
    "${UV}" venv --python "${PYTHON_VERSION}" "${VENV}"
}

install_jansky() {
    # jansky is not on PyPI — it comes from its git tag, BEFORE the wheel, so the
    # wheel's `jansky` requirement is already satisfied and never hits PyPI.
    log "installing jansky @ ${JANSKY_REF} from git"
    "${UV}" pip install --python "${VENV}/bin/python" \
        "jansky @ git+${JANSKY_GIT_URL}@${JANSKY_REF}"
}

resolve_release_version() {
    # Latest release tag via the /releases/latest redirect (no API token, no rate-limit JSON).
    local effective
    effective="$(curl -fsSLI -o /dev/null -w '%{url_effective}' "https://github.com/${REPO}/releases/latest")" \
        || die "could not resolve the latest release of ${REPO}"
    VERSION="${effective##*/tag/}"
    [[ "${VERSION}" == v* ]] || die "could not parse a release tag from ${effective}"
    log "latest release: ${VERSION}"
}

fetch_wheel() {
    local wheel_name url
    wheel_name="jansky_observe-${VERSION#v}-py3-none-any.whl"
    url="https://github.com/${REPO}/releases/download/${VERSION}/${wheel_name}"
    WHEEL_PATH="$(mktemp -d)/${wheel_name}"
    log "downloading ${url}"
    curl -fsSL -o "${WHEEL_PATH}" "${url}" || die "could not download ${url}"
}

install_wheel() {
    [[ -f "${WHEEL_PATH}" ]] || die "wheel not found: ${WHEEL_PATH}"
    log "installing $(basename "${WHEEL_PATH}")"
    # --reinstall-package: a re-run with the same version is still an in-place upgrade.
    "${UV}" pip install --python "${VENV}/bin/python" \
        --reinstall-package jansky-observe "${WHEEL_PATH}"
}

install_udev_rules() {
    log "writing ${UDEV_RULES_FILE}"
    # /etc/udev/rules.d may not exist where udev isn't installed (pristine container).
    install -d "$(dirname "${UDEV_RULES_FILE}")"
    print_udev > "${UDEV_RULES_FILE}"
    # Reload only when the udev daemon is actually running (not in a container).
    if have udevadm && [[ -d /run/udev ]]; then
        udevadm control --reload-rules
        # usb for the SDRs; tty for the rotator's USB-serial bridge (M9).
        udevadm trigger --subsystem-match=usb --subsystem-match=tty || true
    fi
}

install_default_file() {
    # Written ONLY if absent — re-running the installer (an in-place upgrade) must never
    # clobber the operator's source choice.
    if [[ -e "${DEFAULT_FILE}" ]]; then
        log "keeping existing ${DEFAULT_FILE}"
        return 0
    fi
    log "writing ${DEFAULT_FILE}"
    install -d "$(dirname "${DEFAULT_FILE}")"
    cat > "${DEFAULT_FILE}" <<'DEFAULT_EOF'
# jansky-observe capture daemon settings — read by jansky-observe-capture.service.
#
# Source:
#   synthetic  noise + fake-HI frames, no hardware (the installed default)
#   airspy     the real Airspy Mini — switch to this at first light, then
#              `sudo systemctl restart jansky-observe-capture`
JANSKY_OBSERVE_SOURCE=synthetic

# Frame rate (frames/second). Each waterfall row is an *integrated* spectrum, so
# 4 fps is a spectrometer cadence, not a rendering limit: raising it trades
# integration time per row for smoother on-screen motion. Restart the daemon
# after changing. (The browser can also interpolate the scroll cosmetically.)
JANSKY_OBSERVE_FPS=4.0
DEFAULT_EOF
}

install_units() {
    log "writing systemd units to ${UNIT_DIR}"
    install -d "${UNIT_DIR}"
    print_unit_server > "${UNIT_DIR}/jansky-observe.service"
    print_unit_capture > "${UNIT_DIR}/jansky-observe-capture.service"
}

start_units() {
    systemctl daemon-reload
    systemctl enable jansky-observe.service jansky-observe-capture.service
    if [[ "${START}" -eq 0 ]]; then
        log "--no-start: services enabled but not started"
        return 0
    fi
    log "starting services"
    systemctl restart jansky-observe-capture.service
    systemctl restart jansky-observe.service
}

check_version_cli() {
    log "checking ${VENV}/bin/jansky-observe --version"
    "${VENV}/bin/jansky-observe" --version
}

wait_healthz() {
    local i
    log "waiting for ${HEALTH_URL} (up to ${HEALTH_TIMEOUT}s)"
    for ((i = 0; i < HEALTH_TIMEOUT; i++)); do
        if curl -fsS "${HEALTH_URL}" >/dev/null 2>&1; then
            log "healthz OK"
            return 0
        fi
        sleep 1
    done
    return 1
}

# --- container/CI smoke mode -----------------------------------------------
# Used when systemd is not managing the machine (release install gate runs in a
# pristine debian:trixie container) or when --smoke is passed explicitly: run both
# processes in the foreground session, health-check, and read ONE binary frame from
# the live WebSocket to prove the whole synthetic pipe (daemon -> ZMQ -> server -> WS).

SMOKE_PIDS=()
SMOKE_LOG_DIR=""

smoke_cleanup() {
    local pid
    for pid in "${SMOKE_PIDS[@]:-}"; do
        [[ -n "${pid}" ]] && kill "${pid}" 2>/dev/null || true
    done
    wait 2>/dev/null || true
}

run_smoke() {
    local run_as=() capture_pid server_pid
    SMOKE_LOG_DIR="$(mktemp -d)"
    trap smoke_cleanup EXIT
    # Run as the service user when possible — same identity systemd would use.
    if have runuser; then run_as=(runuser -u "${SERVICE_USER}" --); fi

    log "smoke: starting capture daemon (--synthetic) and API server in the background"
    (
        cd "${DATA_DIR}"
        exec "${run_as[@]}" env JANSKY_OBSERVE_ZMQ_ENDPOINT=tcp://127.0.0.1:8410 \
            "${VENV}/bin/jansky-observe-capture" --synthetic
    ) >"${SMOKE_LOG_DIR}/capture.log" 2>&1 </dev/null &
    capture_pid=$!
    (
        cd "${DATA_DIR}"
        exec "${run_as[@]}" env JANSKY_OBSERVE_ZMQ_ENDPOINT=tcp://127.0.0.1:8410 \
            "${VENV}/bin/jansky-observe" --host 0.0.0.0 --port 8000
    ) >"${SMOKE_LOG_DIR}/server.log" 2>&1 </dev/null &
    server_pid=$!
    SMOKE_PIDS=("${capture_pid}" "${server_pid}")

    if ! wait_healthz; then
        warn "healthz never came up; logs:"
        tail -n 50 "${SMOKE_LOG_DIR}/capture.log" "${SMOKE_LOG_DIR}/server.log" >&2 || true
        die "smoke test failed (healthz timeout)"
    fi
    kill -0 "${capture_pid}" 2>/dev/null || die "smoke: capture daemon exited early (see ${SMOKE_LOG_DIR}/capture.log)"
    kill -0 "${server_pid}" 2>/dev/null || die "smoke: API server exited early (see ${SMOKE_LOG_DIR}/server.log)"

    log "smoke: reading one binary frame from ws://127.0.0.1:8000/ws/live"
    cat > "${SMOKE_LOG_DIR}/ws_smoke.py" <<'PY_EOF'
"""Synthetic capture smoke: one non-empty binary frame from the live WebSocket."""

import asyncio

import websockets  # a runtime dependency of jansky-observe, so already in the venv


async def main() -> None:
    async with websockets.connect(
        "ws://127.0.0.1:8000/ws/live", open_timeout=15, close_timeout=5
    ) as ws:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 30
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise SystemExit("smoke: timed out waiting for a binary frame")
            msg = await asyncio.wait_for(ws.recv(), timeout=remaining)
            if isinstance(msg, (bytes, bytearray)):
                if len(msg) == 0:
                    raise SystemExit("smoke: received an EMPTY binary frame")
                print(f"smoke: got one binary frame ({len(msg)} bytes)")
                return


asyncio.run(main())
PY_EOF
    "${VENV}/bin/python" "${SMOKE_LOG_DIR}/ws_smoke.py" || {
        warn "WebSocket smoke failed; logs:"
        tail -n 50 "${SMOKE_LOG_DIR}/capture.log" "${SMOKE_LOG_DIR}/server.log" >&2 || true
        die "smoke test failed (WebSocket frame)"
    }
    log "smoke: cleaning up background processes"
    smoke_cleanup
    trap - EXIT
    SMOKE_PIDS=()
    log "smoke test PASSED"
}

set_source() {
    # First-light switch (and back): update /etc/default/jansky-observe in place and
    # bounce the capture daemon. The API server (and the observation record with it)
    # stays up throughout — the daemon is the only thing restarting.
    local src="$1"
    case "${src}" in
        synthetic | airspy) ;;
        *) die "--set-source takes 'synthetic' or 'airspy', got '${src}'" ;;
    esac
    install_default_file
    sed -i "s/^JANSKY_OBSERVE_SOURCE=.*/JANSKY_OBSERVE_SOURCE=${src}/" "${DEFAULT_FILE}"
    log "capture source set to '${src}' in ${DEFAULT_FILE}"
    if systemd_running; then
        systemctl restart jansky-observe-capture.service
        log "jansky-observe-capture restarted: $(systemctl is-active jansky-observe-capture.service)"
    else
        warn "systemd not running — restart the capture daemon yourself"
    fi
}

# --- Argon ONE V5 M.2 NVMe case (--install-argon) --------------------------
# Optional convenience for the physical build. Entirely independent of the SDR/capture
# path — it touches config.txt, the bootloader EEPROM and Argon's own daemon, and never
# goes near the bias-tee-guarded code. Everything here is idempotent.

boot_config_path() {
    # Echo the active config.txt (firmware partition on Trixie, /boot on older layouts).
    local p
    for p in "${BOOT_CONFIG_CANDIDATES[@]}"; do
        [[ -f "${p}" ]] && { printf '%s' "${p}"; return 0; }
    done
    return 1
}

ensure_config_line() {
    # Idempotently ensure a config.txt line: leave it if already exact, normalize a
    # different/commented form of the same setting, else append.
    #   ensure_config_line <file> <match-regex-for-whole-line> <desired-line>
    local file="$1" match="$2" line="$3"
    if grep -qxF "${line}" "${file}"; then
        log "  ${file}: '${line}' already set"
        return 0
    fi
    if grep -qE "${match}" "${file}"; then
        sed -i -E "s|${match}|${line}|" "${file}"
        log "  ${file}: normalized to '${line}'"
    else
        printf '%s\n' "${line}" >> "${file}"
        log "  ${file}: appended '${line}'"
    fi
}

install_argon_daemon() {
    # Argon40's own installer for the case fan + power button. It speaks the case MCU's
    # protocol, so only Argon ships it; we fetch-then-run (rather than piping straight to
    # bash) so the download failure is caught and the script is on disk if it errors.
    have curl || die "curl is required to fetch the Argon installer"
    log "installing the Argon ONE V5 fan + power-button daemon"
    log "  source: ${ARGON_INSTALLER_URL} (Argon40 official; adds 'argonone-config' + argononed.service)"
    local tmp
    tmp="$(mktemp)"
    curl -fsSL -o "${tmp}" "${ARGON_INSTALLER_URL}" \
        || die "could not download the Argon installer (${ARGON_INSTALLER_URL}) — check networking"
    bash "${tmp}" || die "the Argon installer failed — see its output above"
    rm -f "${tmp}"
    log "Argon daemon installed — tune the fan curve any time with: argonone-config"
}

configure_argon_nvme_boot() {
    # NVMe *boot* (vs. just seeing the NVMe as a data disk): bring the bootloader up to date
    # and set an NVMe-first boot order. Boot-critical, so opt-in (--argon-nvme-boot) and
    # confirmed on a TTY (or --yes).
    have rpi-eeprom-config \
        || die "rpi-eeprom-config not found — 'apt-get install rpi-eeprom' first, or drop --argon-nvme-boot"
    if [[ "${ASSUME_YES}" -ne 1 ]]; then
        if [[ -t 0 ]]; then
            local reply
            printf 'This rewrites the Pi bootloader EEPROM (boot order -> NVMe-first).\n' >&2
            read -r -p "Type 'nvme' to confirm: " reply
            [[ "${reply}" == "nvme" ]] || die "not confirmed — EEPROM left unchanged"
        else
            die "--argon-nvme-boot rewrites the bootloader EEPROM and stdin is not a TTY — re-run with --yes"
        fi
    fi
    if have rpi-eeprom-update; then
        log "updating the Raspberry Pi bootloader to the latest (rpi-eeprom-update -a)"
        rpi-eeprom-update -a || warn "rpi-eeprom-update returned non-zero (may already be current)"
    fi
    local cfg
    cfg="$(mktemp)"
    rpi-eeprom-config > "${cfg}"
    # BOOT_ORDER nibbles read right-to-left: 6=NVMe, 4=USB-MSD, 1=SD, f=restart the sequence.
    # 0xf416 => NVMe first, then USB, then SD, then loop. PCIE_PROBE=1 helps third-party
    # M.2 boards (like Argon's) enumerate before the boot attempt.
    local k
    for k in "BOOT_ORDER=0xf416" "PCIE_PROBE=1"; do
        if grep -qE "^${k%%=*}=" "${cfg}"; then
            sed -i -E "s|^${k%%=*}=.*|${k}|" "${cfg}"
        else
            printf '%s\n' "${k}" >> "${cfg}"
        fi
    done
    log "applying EEPROM config (BOOT_ORDER=0xf416, PCIE_PROBE=1)"
    rpi-eeprom-config --apply "${cfg}" || die "rpi-eeprom-config --apply failed"
    rm -f "${cfg}"
    log "bootloader set to try the NVMe first — the new order takes effect on next reboot"
}

install_argon_case() {
    # Two independent, idempotent halves: (1) enable the Pi 5 PCIe/M.2 slot so the NVMe is
    # detected; (2) install Argon's fan + button daemon. --argon-nvme-boot adds the separate
    # boot-order step. Reboot afterwards for the PCIe change to take effect.
    local model=""
    [[ -r /proc/device-tree/model ]] && model="$(tr -d '\0' < /proc/device-tree/model)"
    if [[ "${ALLOW_UNSUPPORTED}" -ne 1 && "${model}" != *"Raspberry Pi 5"* ]]; then
        die "hardware looks like '${model:-unknown}', not a Raspberry Pi 5 — the Argon ONE V5 case is Pi 5 only (--allow-unsupported-os to override)"
    fi

    # (1) PCIe / M.2 enablement in config.txt. On the Pi 5 the external PCIe lane is off by
    # default; dtparam=pciex1 turns it on and pciex1_gen=3 runs the Argon M.2 board at Gen3.
    local cfg
    if cfg="$(boot_config_path)"; then
        log "enabling the PCIe M.2 slot in ${cfg}"
        ensure_config_line "${cfg}" '^[#[:space:]]*dtparam=pciex1$' 'dtparam=pciex1'
        ensure_config_line "${cfg}" '^[#[:space:]]*dtparam=pciex1_gen=.*' 'dtparam=pciex1_gen=3'
    else
        warn "no config.txt found (${BOOT_CONFIG_CANDIDATES[*]}) — enable 'dtparam=pciex1' + 'dtparam=pciex1_gen=3' by hand"
    fi

    # (2) Argon fan + power-button daemon.
    install_argon_daemon

    # (3) Optional: make the Pi boot from the NVMe.
    if [[ "${ARGON_NVME_BOOT}" -eq 1 ]]; then
        configure_argon_nvme_boot
    else
        log "skipping NVMe boot-order change (pass --argon-nvme-boot to boot from the NVMe)"
    fi

    log "Argon ONE V5 setup done. REBOOT to bring up the PCIe/M.2 slot: sudo reboot"
    log "after reboot, confirm the NVMe with: lsblk -d -o NAME,SIZE,MODEL | grep -i nvme"
}

do_reset_data() {
    # QA / clean-install reset (README "Resetting station data"): delete ALL station
    # data — DB, captures, photos, reports — and let migrations + seeds rebuild a
    # fresh station on the next start. Deliberately all-or-nothing: there is no
    # per-observation delete anywhere (an observing log you can edit selectively
    # isn't a log; the MCP surface carries no delete verbs either — plan §12.4).
    [[ -d "${DATA_DIR}" ]] || die "${DATA_DIR} does not exist — nothing to reset"
    if [[ "${ASSUME_YES}" -ne 1 ]]; then
        [[ -t 0 ]] || die "--reset-data is destructive and stdin is not a TTY — re-run with --yes to confirm"
        local reply
        printf 'This permanently deletes ALL observations, captures, photos and reports in %s.\n' "${DATA_DIR}" >&2
        read -r -p "Type 'reset' to confirm: " reply
        [[ "${reply}" == "reset" ]] || die "not confirmed — nothing was deleted"
    fi
    local restart=0
    if systemd_running && have systemctl; then
        log "stopping services"
        systemctl stop jansky-observe.service jansky-observe-capture.service 2>/dev/null || true
        restart=1
    fi
    log "deleting the contents of ${DATA_DIR}"
    find "${DATA_DIR}" -mindepth 1 -delete
    if [[ "${restart}" -eq 1 ]]; then
        log "restarting services (a fresh DB is migrated + seeded on start)"
        systemctl start jansky-observe-capture.service jansky-observe.service
        wait_healthz \
            || die "health check failed after reset — see: journalctl -u jansky-observe -u jansky-observe-capture"
    fi
    log "station data reset — ${DATA_DIR} is fresh"
}

do_uninstall() {
    log "uninstalling jansky-observe (data in ${DATA_DIR} is kept)"
    if systemd_running && have systemctl; then
        systemctl disable --now jansky-observe.service jansky-observe-capture.service 2>/dev/null || true
    fi
    rm -f "${UNIT_DIR}/jansky-observe.service" "${UNIT_DIR}/jansky-observe-capture.service"
    if systemd_running && have systemctl; then
        systemctl daemon-reload
    fi
    rm -f "${UDEV_RULES_FILE}"
    if have udevadm && [[ -d /run/udev ]]; then
        udevadm control --reload-rules || true
    fi
    rm -rf "${PREFIX}"
    log "removed units, udev rules and ${PREFIX}."
    log "kept: ${DATA_DIR} (observation data), ${DEFAULT_FILE} (source choice) and the '${SERVICE_USER}' user."
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

main() {
    local VERSION="" WHEEL_PATH="" JANSKY_REF="${DEFAULT_JANSKY_REF}"
    local START=1 SMOKE=0 UNINSTALL=0 ALLOW_UNSUPPORTED=0 SET_SOURCE=""
    local RESET_DATA=0 ASSUME_YES=0 INSTALL_ARGON=0 ARGON_NVME_BOOT=0
    local UV=""

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --version) VERSION="${2:?--version needs an argument, e.g. v0.1.0}"; shift 2 ;;
            --wheel) WHEEL_PATH="${2:?--wheel needs a path}"; shift 2 ;;
            --jansky-ref) JANSKY_REF="${2:?--jansky-ref needs a git ref}"; shift 2 ;;
            --no-start) START=0; shift ;;
            --smoke) SMOKE=1; shift ;;
            --uninstall) UNINSTALL=1; shift ;;
            --reset-data) RESET_DATA=1; shift ;;
            --yes) ASSUME_YES=1; shift ;;
            --set-source) SET_SOURCE="${2:?--set-source needs 'synthetic' or 'airspy'}"; shift 2 ;;
            --install-argon) INSTALL_ARGON=1; shift ;;
            --argon-nvme-boot) ARGON_NVME_BOOT=1; shift ;;
            --allow-unsupported-os) ALLOW_UNSUPPORTED=1; shift ;;
            # Drift-check subcommands: print embedded assets and exit (no root needed).
            --print-unit)
                case "${2:?--print-unit needs 'server' or 'capture'}" in
                    server) print_unit_server ;;
                    capture) print_unit_capture ;;
                    *) die "--print-unit takes 'server' or 'capture', got '$2'" ;;
                esac
                exit 0 ;;
            --print-udev) print_udev; exit 0 ;;
            -h | --help) usage; exit 0 ;;
            *) usage >&2; die "unknown flag: $1" ;;
        esac
    done

    [[ "$(id -u)" -eq 0 ]] || die "must run as root (sudo bash install.sh ...)"

    if [[ -n "${SET_SOURCE}" ]]; then
        set_source "${SET_SOURCE}"
        exit 0
    fi

    if [[ "${INSTALL_ARGON}" -eq 1 ]]; then
        install_argon_case
        exit 0
    fi

    if [[ "${RESET_DATA}" -eq 1 ]]; then
        do_reset_data
        exit 0
    fi

    if [[ "${UNINSTALL}" -eq 1 ]]; then
        do_uninstall
        exit 0
    fi

    if [[ "${ALLOW_UNSUPPORTED}" -eq 1 ]]; then
        warn "--allow-unsupported-os: skipping the OS/architecture check"
    else
        check_os
    fi

    apt_install
    ensure_uv
    ensure_user_and_dirs
    ensure_venv
    install_jansky

    if [[ -z "${WHEEL_PATH}" ]]; then
        [[ -n "${VERSION}" ]] || resolve_release_version
        fetch_wheel
    fi
    install_wheel

    install_udev_rules
    install_default_file
    install_units
    check_version_cli

    if [[ "${SMOKE}" -eq 1 ]] || ! systemd_running; then
        if [[ "${SMOKE}" -eq 0 ]]; then
            warn "systemd is not managing this machine — falling back to foreground smoke mode"
        fi
        if [[ "${START}" -eq 0 ]]; then
            log "--no-start: skipping the foreground smoke run"
        else
            run_smoke
        fi
    else
        start_units
        if [[ "${START}" -eq 1 ]]; then
            wait_healthz || die "health check failed — see: journalctl -u jansky-observe -u jansky-observe-capture"
        fi
    fi

    log "done. jansky-observe is installed in ${VENV}; data lives in ${DATA_DIR}."
}

main "$@"
