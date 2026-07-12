#!/usr/bin/env bash
# deploy/qemu/run-install-test.sh — full-fidelity install gate (`make qemu-install`, plan §9).
#
# Boots the PINNED genuine Raspberry Pi OS Lite 64-bit image (deploy/OS_IMAGE) headless in
# qemu-system-aarch64 (-M virt -cpu cortex-a76, the Pi 5's core), runs the real
# deploy/install.sh inside the guest over SSH, and asserts the same health checks the
# installer promises (GET /healthz + `jansky-observe --version` + both systemd units).
#
# KERNEL STRATEGY (learned the hard way — do not "simplify" this back):
# The guest USERLAND is the genuine pinned Pi OS image; the KERNEL is a stock Alpine
# netboot vmlinuz-lts + initramfs-lts (pinned below), which boot the Pi OS rootfs via
# root= + switch_root and bring virtio drivers with them. Why not the image's own kernel:
#   - -M virt: the Pi kernel has no virtio and no generic-PCI host driver — virt can
#     offer it no disk or NIC at all (initramfs: "ALERT! /dev/vda2 does not exist").
#   - -M raspi4b: QEMU disables the 4B's PCIe (where its xhci USB host lives) and the 4B
#     DTB puts dwc2 in peripheral mode — no usable NIC.
#   - -M raspi3b: boots, but the Trixie 6.18 Pi kernel runs at ~1/100 real time under
#     QEMU's board emulation (guest clock advanced ~1.4 s per 120 wall-seconds) — an
#     install would take days. Also: a usb-net device present at boot wedges the
#     initramfs in a USB re-enumeration loop.
# The Alpine netboot initramfs is a plain downloadable file with virtio + ext4 modules
# and an init that supports root= disk boot — no extraction, no root privileges needed.
#
# When to run (release-blocking, plan §9): REQUIRED before tagging v0.1.0 and whenever
# deploy/install.sh or deploy/OS_IMAGE changes; optional otherwise (too slow for every push).
#
# HONEST LIMITS: this gate exercises the genuine Pi OS USERLAND — the exact Debian
# packages, systemd, udev, first-boot user seeding, filesystem layout of the pinned
# image — but NOT the Pi kernel (stock virtio kernel instead, see above), NOT Pi 5
# silicon (BCM2712; QEMU has no Pi 5 machine), and NOT the Pi firmware/EEPROM boot
# chain. Real SDR USB devices are not emulated: SDR hardware smoke (airspy_rx actually
# enumerating on the bus) stays a physical checklist item on the real Pi.
#
# Requirements (Fedora hints; the check below tells you what is missing):
#   sudo dnf install qemu-system-aarch64 qemu-img mtools sshpass openssl xz
# guestfish (libguestfs) is preferred for image surgery; without it, mtools (rootless,
# FAT boot partition only — sufficient) is used, then `sudo losetup` + loop mounts.
#
# Usage:
#   deploy/qemu/run-install-test.sh            # download (cached), boot, install, assert
# Env knobs: QEMU_SSH_PORT (default 5022), QEMU_MEM (4096), QEMU_SMP (4),
#            SSH_BOOT_TIMEOUT (900 s).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY_DIR="$(dirname "${SCRIPT_DIR}")"
REPO_DIR="$(dirname "${DEPLOY_DIR}")"
CACHE_DIR="${SCRIPT_DIR}/cache"          # gitignored; survives runs (downloads are slow)
WORK_DIR="${CACHE_DIR}/work"             # per-run scratch (enlarged image, logs)

QEMU_SSH_PORT="${QEMU_SSH_PORT:-5022}"
QEMU_MEM="${QEMU_MEM:-4096}"
QEMU_SMP="${QEMU_SMP:-4}"
SSH_BOOT_TIMEOUT="${SSH_BOOT_TIMEOUT:-900}"
DISK_SIZE="8G"

# Pinned gate kernel (see KERNEL STRATEGY above). Bumping this pin is a gate-only
# change — it does not touch what ships to the Pi — but re-run `make qemu-install`.
GATE_KERNEL_BASE="https://dl-cdn.alpinelinux.org/alpine/v3.22/releases/aarch64/netboot"
GATE_KERNEL_URL="${GATE_KERNEL_BASE}/vmlinuz-lts"
GATE_INITRD_URL="${GATE_KERNEL_BASE}/initramfs-lts"
GATE_MODLOOP_URL="${GATE_KERNEL_BASE}/modloop-lts"   # module source for the ext4 graft

# Modules the Alpine initramfs must PRELOAD for the Pi OS userland. After switch_root,
# the kernel's module autoloader (usermode modprobe) runs against Pi OS's /lib/modules —
# which doesn't match the gate kernel — so anything the boot needs must be loaded here:
#   ext4 (rootfs, grafted in), vfat+nls_* (the /boot/firmware mount; the Alpine kernel's
#   FAT default iocharset is utf8 → nls_utf8, learned from "missing codepage or helper"),
#   af_packet (modular in the Alpine kernel; DHCP needs packet sockets), virtio_net (NIC).
GATE_PRELOAD_MODULES="ext4,vfat,nls_cp437,nls_iso8859_1,nls_utf8,af_packet,virtio_net"

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
    elif command -v mcopy >/dev/null 2>&1 && command -v sfdisk >/dev/null 2>&1; then
        # All the surgery we need is on the FAT boot partition — mtools does it rootless.
        echo "note: guestfish not found; using mtools on the FAT boot partition" >&2
        IMAGE_TOOL="mtools"
    else
        echo "note: neither guestfish (Fedora: sudo dnf install guestfs-tools) nor mtools" \
            "found; falling back to sudo loop mounts" >&2
        IMAGE_TOOL="loop"
        command -v losetup >/dev/null 2>&1 || { echo "missing: losetup (util-linux)" >&2; missing=1; }
    fi
    [[ "${missing}" -eq 0 ]] || die "install the missing tools above and re-run"
}

# ---------------------------------------------------------------------------
# Pinned artifacts: OS image (deploy/OS_IMAGE) + gate kernel, cached locally.
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

fetch_gate_kernel() {
    KERNEL="${CACHE_DIR}/gate-vmlinuz-lts"
    local initrd_orig="${CACHE_DIR}/gate-initramfs-lts"
    local modloop="${CACHE_DIR}/gate-modloop-lts"
    INITRD="${CACHE_DIR}/gate-initramfs-ext4"
    [[ -f "${KERNEL}" ]] || { log "downloading gate kernel"; curl -fsSL -o "${KERNEL}" "${GATE_KERNEL_URL}"; }
    [[ -f "${initrd_orig}" ]] || { log "downloading gate initramfs"; curl -fsSL -o "${initrd_orig}" "${GATE_INITRD_URL}"; }
    [[ -f "${INITRD}" ]] && return 0

    # The netboot initramfs ships NO ext4 (it boots squashfs over the network), so a
    # root=/dev/vda2 mount fails with "No such device". Graft ext4 + its dependency
    # closure from the same release's modloop into an appended cpio overlay.
    # CRITICAL: the initramfs uses kmod's modprobe, which resolves modules through the
    # BINARY indexes (modules.dep.bin / modules.alias.bin) — patching the text
    # modules.dep does nothing ("FATAL: Module ext4 not found in directory …", learned
    # the hard way). So: rebuild the full module tree and re-run depmod over it, then
    # ship the grafted .ko files plus ALL regenerated modules.* indexes in the overlay.
    # Concatenated gzip cpio archives are valid initramfs input; later entries
    # overwrite earlier ones.
    command -v unsquashfs >/dev/null 2>&1 \
        || die "unsquashfs is required to build the gate initramfs (Fedora: sudo dnf install squashfs-tools)"
    command -v depmod >/dev/null 2>&1 \
        || die "depmod is required to build the gate initramfs (Fedora: sudo dnf install kmod)"
    [[ -f "${modloop}" ]] || { log "downloading gate modloop"; curl -fsSL -o "${modloop}" "${GATE_MODLOOP_URL}"; }
    log "building ext4-capable gate initramfs"
    local tmp="${CACHE_DIR}/initrd-build"
    rm -rf "${tmp}"
    mkdir -p "${tmp}/tree" "${tmp}/overlay"

    # Kernel version + ext4 dependency closure, from the modloop's modules.dep.
    unsquashfs -q -n -d "${tmp}/dep" "${modloop}" 'modules/*/modules.dep' >/dev/null
    local kver depfile
    kver="$(ls -1 "${tmp}/dep/modules" | head -n1)"
    depfile="${tmp}/dep/modules/${kver}/modules.dep"
    local depline
    depline="$(grep -E '^kernel/fs/ext4/ext4\.ko' "${depfile}")" \
        || die "ext4 not found in modloop modules.dep"
    local all_mods
    all_mods="${depline%%:*} ${depline#*:}"

    # Full module tree = the original initramfs's lib/modules + the grafted modules.
    (cd "${tmp}/tree" && zcat "${initrd_orig}" | cpio -id --quiet 'lib/modules/*')
    [[ -d "${tmp}/tree/lib/modules/${kver}" ]] \
        || die "gate kernel/modloop version mismatch (initramfs has no ${kver})"
    local f extract_list=()
    for f in ${all_mods}; do extract_list+=("modules/${kver}/${f}"); done
    unsquashfs -q -n -d "${tmp}/mods" "${modloop}" "${extract_list[@]}" >/dev/null
    for f in ${all_mods}; do
        mkdir -p "${tmp}/tree/lib/modules/${kver}/$(dirname "${f}")"
        cp "${tmp}/mods/modules/${kver}/${f}" "${tmp}/tree/lib/modules/${kver}/${f}"
    done

    # Regenerate every index over the merged tree (host depmod is arch-neutral).
    depmod -b "${tmp}/tree" "${kver}"

    # Overlay = grafted .ko files + all regenerated modules.* metadata.
    for f in ${all_mods}; do
        mkdir -p "${tmp}/overlay/lib/modules/${kver}/$(dirname "${f}")"
        cp "${tmp}/tree/lib/modules/${kver}/${f}" "${tmp}/overlay/lib/modules/${kver}/${f}"
    done
    cp "${tmp}/tree/lib/modules/${kver}"/modules.* "${tmp}/overlay/lib/modules/${kver}/"

    (cd "${tmp}/overlay" && find . -mindepth 1 | cpio -o -H newc --quiet | gzip) \
        > "${tmp}/overlay.cpio.gz"
    cat "${initrd_orig}" "${tmp}/overlay.cpio.gz" > "${INITRD}"
    rm -rf "${tmp}"
    log "gate initramfs ready: ${INITRD}"
}

prepare_disk() {
    mkdir -p "${WORK_DIR}"
    # Clear last run's guest console up front — anything watching the log must never
    # see stale content from a previous run (qemu only truncates it at boot time).
    : > "${WORK_DIR}/console.log"
    DISK_IMG="${WORK_DIR}/test.img"
    log "decompressing to ${DISK_IMG}"
    xz -dc "${IMAGE_XZ}" > "${DISK_IMG}"
    log "enlarging image to ${DISK_SIZE} (grow_rootfs expands the partition in-guest)"
    qemu-img resize -f raw "${DISK_IMG}" "${DISK_SIZE}" >/dev/null
}

# ---------------------------------------------------------------------------
# Image surgery: pre-seed the first-boot user + SSH on the FAT boot partition.
# Preferred: guestfish (no root). Then mtools (no root). Fallback: sudo loop mount.
# ---------------------------------------------------------------------------

write_userconf() {
    # Raspberry Pi OS first-boot user pre-seed: userconf.txt = 'user:crypted-password',
    # plus an empty `ssh` file to enable sshd — the same mechanism Raspberry Pi Imager uses.
    USERCONF_FILE="${WORK_DIR}/userconf.txt"
    printf '%s:%s\n' "${GUEST_USER}" "$(openssl passwd -6 "${GUEST_PASS}")" > "${USERCONF_FILE}"
}

surgery_guestfish() {
    guestfish -a "${DISK_IMG}" -m /dev/sda1 <<GF_EOF
upload ${USERCONF_FILE} /userconf.txt
touch /ssh
GF_EOF
}

surgery_mtools() {
    # mtools addresses a partition inside a raw image as <file>@@<byte offset>.
    local start spec
    start="$(sfdisk -d "${DISK_IMG}" | sed -n 's/^[^ ]*1 : start= *\([0-9]\+\),.*/\1/p')"
    [[ -n "${start}" ]] || die "could not find the boot-partition offset (sfdisk -d)"
    spec="${DISK_IMG}@@$((start * 512))"
    mcopy -o -i "${spec}" "${USERCONF_FILE}" ::/userconf.txt
    : > "${WORK_DIR}/ssh"
    mcopy -o -i "${spec}" "${WORK_DIR}/ssh" ::/ssh
}

surgery_loop() {
    log "loop-mount fallback needs sudo for losetup/mount"
    local loop mnt
    loop="$(sudo losetup -Pf --show "${DISK_IMG}")"
    mnt="$(mktemp -d)"
    sudo mount "${loop}p1" "${mnt}"
    sudo cp "${USERCONF_FILE}" "${mnt}/userconf.txt"
    sudo touch "${mnt}/ssh"
    sudo umount "${mnt}"
    sudo losetup -d "${loop}"
}

image_surgery() {
    write_userconf
    case "${IMAGE_TOOL}" in
        guestfish) surgery_guestfish ;;
        mtools) surgery_mtools ;;
        *) surgery_loop ;;
    esac
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
    # The Alpine initramfs loads virtio_blk/virtio_net (modules= + device uevents),
    # mounts /dev/vda2 (the Pi OS rootfs) and switch_roots into its systemd. The pl011
    # serial console works on virt, so boot progress lands in ${CONSOLE_LOG}.
    qemu-system-aarch64 \
        -M virt -cpu cortex-a76 -smp "${QEMU_SMP}" -m "${QEMU_MEM}" \
        -kernel "${KERNEL}" -initrd "${INITRD}" \
        -append "root=/dev/vda2 rootfstype=ext4 rw modules=${GATE_PRELOAD_MODULES} console=ttyAMA0,115200" \
        -drive "if=none,file=${DISK_IMG},format=raw,id=hd0" \
        -device virtio-blk-pci,drive=hd0 \
        -netdev "user,id=net0,hostfwd=tcp::${QEMU_SSH_PORT}-:22" \
        -device virtio-net-pci,netdev=net0 \
        -display none \
        -monitor "unix:${WORK_DIR}/monitor.sock,server,nowait" \
        -serial "file:${CONSOLE_LOG}" &
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
    log "waiting for SSH (first boot seeds the user + host keys; up to ${SSH_BOOT_TIMEOUT}s)"
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

enable_passwordless_sudo() {
    # The Trixie image's first-boot user does NOT get NOPASSWD sudo, and our SSH
    # commands have no TTY for a sudo password prompt. Bootstrap NOPASSWD once via
    # `sudo -S` (password on stdin); everything after this uses plain sudo.
    log "enabling passwordless sudo for ${GUEST_USER} in the guest"
    printf '%s\n' "${GUEST_PASS}" | guest_ssh \
        "sudo -S -p '' sh -c 'echo \"${GUEST_USER} ALL=(ALL) NOPASSWD: ALL\" > /etc/sudoers.d/99-qemu-gate && chmod 440 /etc/sudoers.d/99-qemu-gate'"
    guest_ssh sudo -n true || die "passwordless sudo bootstrap failed"
}

grow_rootfs() {
    # We bypass Pi OS's cmdline.txt first-boot resize hook (init=…/init_resize.sh) by
    # booting the gate kernel directly, so the rootfs would stay at image size (~2.7 GB)
    # — not enough for the venv. Grow partition 2 + ext4 online instead, deriving the
    # disk from wherever / is actually mounted (vda2, mmcblk0p2, sda2, …).
    log "growing the root partition to fill the ${DISK_SIZE} disk"
    guest_ssh 'set -e
        root_src="$(findmnt -no SOURCE /)"
        disk="${root_src%2}"; disk="${disk%p}"
        echo ", +" | sudo sfdisk -N 2 --no-reread --force "${disk}" >/dev/null 2>&1 || true
        sudo partx -u "${disk}" 2>/dev/null || true
        sudo resize2fs "${root_src}"
        df -h --output=size,avail / | tail -1'
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
    fetch_gate_kernel
    prepare_disk
    image_surgery
    boot_qemu
    wait_for_ssh
    enable_passwordless_sudo
    grow_rootfs
    run_install_in_guest
    assert_health
    log "RESULT: PASS — install.sh works on the pinned image (${OS_IMAGE_NAME})"
}

main "$@"
