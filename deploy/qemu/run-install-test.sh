#!/usr/bin/env bash
# deploy/qemu/run-install-test.sh — full-fidelity install gate (`make qemu-install`, plan §9).
#
# Boots the PINNED genuine Raspberry Pi OS Lite 64-bit image (deploy/OS_IMAGE) headless in
# qemu-system-aarch64 (-M virt -cpu cortex-a76, the Pi 5's core), runs the real
# deploy/install.sh inside the guest over SSH, and asserts the same health checks the
# installer promises (GET /healthz + `jansky-observe --version`).
#
# When to run (release-blocking, plan §9): REQUIRED before tagging v0.1.0 and whenever
# deploy/install.sh or deploy/OS_IMAGE changes; optional otherwise (too slow for every push).
#
# HONEST LIMITS: QEMU emulates the OS and userland — the exact Debian packages, systemd,
# udev, users, filesystem layout of the pinned image — but NOT Pi 5 silicon (BCM2712,
# firmware boot chain) and NOT USB devices. SDR hardware smoke (airspy_rx actually
# enumerating on the bus) stays a physical checklist item on the real Pi. We also boot the
# image's kernel8.img directly on the `virt` machine rather than the Pi firmware path.
#
# Requirements (Fedora hints; the check below tells you what is missing):
#   sudo dnf install qemu-system-aarch64 qemu-img guestfs-tools sshpass openssl xz
# guestfish (libguestfs) is preferred for image surgery; without it the script falls back
# to `sudo losetup` + loop mounts.
#
# Usage:
#   deploy/qemu/run-install-test.sh            # download (cached), boot, install, assert
# Env knobs: QEMU_SSH_PORT (default 5022), QEMU_MEM (2048), QEMU_SMP (4),
#            SSH_BOOT_TIMEOUT (900 s — first boot resizes the rootfs and seeds the user).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY_DIR="$(dirname "${SCRIPT_DIR}")"
REPO_DIR="$(dirname "${DEPLOY_DIR}")"
CACHE_DIR="${SCRIPT_DIR}/cache"          # gitignored; survives runs (image download is slow)
WORK_DIR="${CACHE_DIR}/work"             # per-run scratch (enlarged image, kernel, logs)

QEMU_SSH_PORT="${QEMU_SSH_PORT:-5022}"
QEMU_MEM="${QEMU_MEM:-2048}"
QEMU_SMP="${QEMU_SMP:-4}"
SSH_BOOT_TIMEOUT="${SSH_BOOT_TIMEOUT:-900}"
DISK_SIZE="8G"

GUEST_USER="pi"
# Throwaway credential for a local, NAT-only, single-run VM — not a secret.
GUEST_PASS="jansky-qemu-test"

log() { printf '\033[1;34m[qemu-install]\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m[qemu-install]\033[0m ERROR: %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# Tool availability — checked up front with install hints (Fedora).
# ---------------------------------------------------------------------------

require_tools() {
    local missing=0
    for tool in qemu-system-aarch64:qemu-system-aarch64 qemu-img:qemu-img \
        sshpass:sshpass openssl:openssl xz:xz curl:curl ssh:openssh-clients \
        scp:openssh-clients sha256sum:coreutils; do
        local bin="${tool%%:*}" pkg="${tool##*:}"
        if ! command -v "${bin}" >/dev/null 2>&1; then
            echo "missing: ${bin}  (Fedora: sudo dnf install ${pkg})" >&2
            missing=1
        fi
    done
    if command -v guestfish >/dev/null 2>&1; then
        IMAGE_TOOL="guestfish"
    else
        echo "note: guestfish not found (Fedora: sudo dnf install guestfs-tools);" \
            "falling back to sudo loop mounts" >&2
        IMAGE_TOOL="loop"
        command -v losetup >/dev/null 2>&1 || { echo "missing: losetup (util-linux)" >&2; missing=1; }
    fi
    [[ "${missing}" -eq 0 ]] || die "install the missing tools above and re-run"
}

# ---------------------------------------------------------------------------
# Pinned image: read deploy/OS_IMAGE, download + verify into the cache.
# ---------------------------------------------------------------------------

read_os_image_pin() {
    local pin_file="${DEPLOY_DIR}/OS_IMAGE"
    [[ -r "${pin_file}" ]] || die "cannot read ${pin_file}"
    OS_IMAGE_NAME="$(grep '^OS_IMAGE_NAME=' "${pin_file}" | cut -d= -f2-)"
    OS_IMAGE_URL="$(grep '^OS_IMAGE_URL=' "${pin_file}" | cut -d= -f2-)"
    OS_IMAGE_SHA256="$(grep '^OS_IMAGE_SHA256=' "${pin_file}" | cut -d= -f2-)"
    [[ -n "${OS_IMAGE_NAME}" && -n "${OS_IMAGE_URL}" && -n "${OS_IMAGE_SHA256}" ]] \
        || die "incomplete pin in ${pin_file}"
    log "pinned image: ${OS_IMAGE_NAME}"
}

fetch_image() {
    local xz_path="${CACHE_DIR}/${OS_IMAGE_NAME}"
    mkdir -p "${CACHE_DIR}"
    if [[ -f "${xz_path}" ]] \
        && echo "${OS_IMAGE_SHA256}  ${xz_path}" | sha256sum -c --quiet - 2>/dev/null; then
        log "cached image OK: ${xz_path}"
    else
        log "downloading ${OS_IMAGE_URL}"
        curl -fL --progress-bar -o "${xz_path}.part" "${OS_IMAGE_URL}"
        mv "${xz_path}.part" "${xz_path}"
        echo "${OS_IMAGE_SHA256}  ${xz_path}" | sha256sum -c --quiet - \
            || die "SHA-256 mismatch on downloaded image (pin in deploy/OS_IMAGE)"
        log "download verified"
    fi
    IMAGE_XZ="${xz_path}"
}

prepare_disk() {
    mkdir -p "${WORK_DIR}"
    DISK_IMG="${WORK_DIR}/test.img"
    log "decompressing to ${DISK_IMG}"
    xz -dc "${IMAGE_XZ}" > "${DISK_IMG}"
    log "enlarging image to ${DISK_SIZE} (Pi OS auto-expands the rootfs on first boot)"
    qemu-img resize -f raw "${DISK_IMG}" "${DISK_SIZE}" >/dev/null
}

# ---------------------------------------------------------------------------
# Image surgery: pre-seed the first-boot user + SSH, extract kernel/initramfs.
# Preferred: guestfish (no root). Fallback: loop mount with sudo.
# ---------------------------------------------------------------------------

pick_kernel_names() {
    # Boot-partition listing (one name per line) on stdin; picks kernel8/initramfs8
    # (4 KiB pages — the safe choice on QEMU virt) over the Pi-5-only 2712 variants.
    local listing="$1"
    if grep -qx 'kernel8.img' <<<"${listing}"; then
        KERNEL_NAME="kernel8.img" INITRD_NAME="initramfs8"
    elif grep -qx 'kernel_2712.img' <<<"${listing}"; then
        KERNEL_NAME="kernel_2712.img" INITRD_NAME="initramfs_2712"
    else
        die "no kernel8.img/kernel_2712.img in the boot partition — image layout changed?"
    fi
    grep -qx "${INITRD_NAME}" <<<"${listing}" \
        || die "kernel ${KERNEL_NAME} found but ${INITRD_NAME} missing in the boot partition"
    log "using ${KERNEL_NAME} + ${INITRD_NAME}"
}

write_userconf() {
    # Raspberry Pi OS first-boot user pre-seed: userconf.txt = 'user:crypted-password',
    # plus an empty `ssh` file to enable sshd — the same mechanism Raspberry Pi Imager uses.
    USERCONF_FILE="${WORK_DIR}/userconf.txt"
    printf '%s:%s\n' "${GUEST_USER}" "$(openssl passwd -6 "${GUEST_PASS}")" > "${USERCONF_FILE}"
}

surgery_guestfish() {
    local listing
    listing="$(guestfish --ro -a "${DISK_IMG}" -m /dev/sda1 ls /)"
    pick_kernel_names "${listing}"
    guestfish -a "${DISK_IMG}" -m /dev/sda1 <<GF_EOF
upload ${USERCONF_FILE} /userconf.txt
touch /ssh
copy-out /${KERNEL_NAME} ${WORK_DIR}
copy-out /${INITRD_NAME} ${WORK_DIR}
GF_EOF
}

surgery_loop() {
    log "loop-mount fallback needs sudo for losetup/mount"
    local loop mnt listing
    loop="$(sudo losetup -Pf --show "${DISK_IMG}")"
    mnt="$(mktemp -d)"
    # shellcheck disable=SC2064
    trap "sudo umount '${mnt}' 2>/dev/null || true; sudo losetup -d '${loop}' 2>/dev/null || true" RETURN
    sudo mount "${loop}p1" "${mnt}"
    listing="$(ls -1 "${mnt}")"
    pick_kernel_names "${listing}"
    sudo cp "${mnt}/${KERNEL_NAME}" "${mnt}/${INITRD_NAME}" "${WORK_DIR}/"
    sudo chown "$(id -u):$(id -g)" "${WORK_DIR}/${KERNEL_NAME}" "${WORK_DIR}/${INITRD_NAME}"
    sudo cp "${USERCONF_FILE}" "${mnt}/userconf.txt"
    sudo touch "${mnt}/ssh"
    sudo umount "${mnt}"
    sudo losetup -d "${loop}"
    trap - RETURN
}

image_surgery() {
    write_userconf
    if [[ "${IMAGE_TOOL}" == "guestfish" ]]; then
        surgery_guestfish
    else
        surgery_loop
    fi
    KERNEL="${WORK_DIR}/${KERNEL_NAME}"
    INITRD="${WORK_DIR}/${INITRD_NAME}"
    [[ -f "${KERNEL}" && -f "${INITRD}" ]] || die "kernel/initramfs extraction failed"
}

# ---------------------------------------------------------------------------
# Boot + SSH + install + assert
# ---------------------------------------------------------------------------

QEMU_PID=""
CONSOLE_LOG=""
cleanup() {
    # Always kill QEMU; keep WORK_DIR (console log + disk) for post-mortem — it is
    # overwritten by the next run and lives in the gitignored cache.
    local status=$?
    if [[ -n "${QEMU_PID}" ]] && kill -0 "${QEMU_PID}" 2>/dev/null; then
        kill "${QEMU_PID}" 2>/dev/null || true
        wait "${QEMU_PID}" 2>/dev/null || true
    fi
    if [[ "${status}" -ne 0 ]]; then
        [[ -n "${CONSOLE_LOG}" ]] && log "guest console log kept at: ${CONSOLE_LOG}"
        printf '\033[1;31m[qemu-install]\033[0m RESULT: FAIL (exit %s)\n' "${status}" >&2
    fi
}
trap cleanup EXIT

boot_qemu() {
    CONSOLE_LOG="${WORK_DIR}/console.log"
    log "booting qemu-system-aarch64 (-M virt -cpu cortex-a76, ssh -> localhost:${QEMU_SSH_PORT})"
    qemu-system-aarch64 \
        -M virt -cpu cortex-a76 -smp "${QEMU_SMP}" -m "${QEMU_MEM}" \
        -kernel "${KERNEL}" -initrd "${INITRD}" \
        -append "root=/dev/vda2 rootfstype=ext4 rw rootwait console=ttyAMA0" \
        -drive "if=none,file=${DISK_IMG},format=raw,id=hd0" \
        -device virtio-blk-device,drive=hd0 \
        -netdev "user,id=net0,hostfwd=tcp::${QEMU_SSH_PORT}-:22" \
        -device virtio-net-device,netdev=net0 \
        -display none -serial "file:${CONSOLE_LOG}" &
    QEMU_PID=$!
}

SSH_OPTS=(
    -p "${QEMU_SSH_PORT}"
    -o StrictHostKeyChecking=no
    -o UserKnownHostsFile=/dev/null
    -o ConnectTimeout=5
    -o PreferredAuthentications=password
    -o PubkeyAuthentication=no
    -o LogLevel=ERROR
)

guest_ssh() { sshpass -p "${GUEST_PASS}" ssh "${SSH_OPTS[@]}" "${GUEST_USER}@localhost" "$@"; }

wait_for_ssh() {
    log "waiting for SSH (first boot resizes the rootfs + seeds the user; up to ${SSH_BOOT_TIMEOUT}s)"
    local waited=0
    until guest_ssh true 2>/dev/null; do
        kill -0 "${QEMU_PID}" 2>/dev/null || die "QEMU exited early — see ${CONSOLE_LOG}"
        sleep 5
        waited=$((waited + 5))
        [[ "${waited}" -lt "${SSH_BOOT_TIMEOUT}" ]] \
            || die "SSH never came up after ${SSH_BOOT_TIMEOUT}s — see ${CONSOLE_LOG}"
    done
    log "SSH is up after ~${waited}s"
}

run_install_in_guest() {
    local scp_files=("${DEPLOY_DIR}/install.sh") install_args=(--jansky-ref v0.1.0)
    # If a locally built wheel exists (make build), gate THAT instead of a published
    # release — this is how the pre-tag check exercises unreleased code.
    local wheel
    wheel="$(ls -1t "${REPO_DIR}"/dist/jansky_observe-*.whl 2>/dev/null | head -n1 || true)"
    if [[ -n "${wheel}" ]]; then
        log "using local wheel: ${wheel}"
        scp_files+=("${wheel}")
        install_args+=(--wheel "/home/${GUEST_USER}/$(basename "${wheel}")")
    else
        log "no local wheel in dist/ — install.sh will download the latest release"
    fi
    sshpass -p "${GUEST_PASS}" scp -P "${QEMU_SSH_PORT}" \
        -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR \
        "${scp_files[@]}" "${GUEST_USER}@localhost:/home/${GUEST_USER}/"
    log "running install.sh in the guest (real systemd path — systemd IS pid 1 here)"
    guest_ssh sudo bash "/home/${GUEST_USER}/install.sh" "${install_args[@]}"
}

assert_health() {
    log "asserting GET /healthz and --version over SSH"
    guest_ssh curl -fsS http://127.0.0.1:8000/healthz
    echo
    guest_ssh /opt/jansky-observe/venv/bin/jansky-observe --version
    guest_ssh systemctl is-active jansky-observe.service jansky-observe-capture.service
}

main() {
    require_tools
    read_os_image_pin
    fetch_image
    prepare_disk
    image_surgery
    boot_qemu
    wait_for_ssh
    run_install_in_guest
    assert_health
    log "RESULT: PASS — install.sh works on the pinned image (${OS_IMAGE_NAME})"
}

main "$@"
