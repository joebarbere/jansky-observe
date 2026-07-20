---
name: radio-quiet
description: Put the Discovery Dish Pi into a radio-quiet state for observing — firmware-disable the onboard Wi-Fi and Bluetooth (dtoverlay=disable-wifi/disable-bt) and their services, reboot, then confirm both radios are truly gone (no wlan0, no hci0) and report. Use when the user says "go radio-quiet", "silence the Pi's radios", or wants to cut onboard RFI before taking readings.
---

# Go radio-quiet: silence the Pi's onboard radios

Disable the Pi's onboard **Wi-Fi and Bluetooth at the firmware level**, reboot, and verify they're
truly off. This is observing hygiene: the 2.4 GHz radios don't land *in* the 1420 MHz HI band, but
they're **bursty local transmitters centimeters from the feed/LNA** whose oscillators and switching
radiate broadband hash and can desense the front end. The firmware `dtoverlay` route unpowers the
radio subsystem entirely — strictly quieter than `rfkill`/`nmcli`, which stop transmit but leave the
module powered and clocked.

## Preconditions — read before touching anything

- **You MUST be reaching the Pi over WIRED ethernet, not Wi-Fi.** Disabling Wi-Fi + rebooting will
  sever any Wi-Fi-based session. Confirm the path is wired first (step 1). If the only link is
  Wi-Fi, **stop** and tell the user to connect ethernet.
- Reach the Pi exactly as `/pi-status` does (`raspberrypi.local` on the LAN, or the direct-link
  address such as `192.168.2.2` / an IPv6 link-local). Record the address you used — you'll need it
  to reconnect after the reboot. Passwordless `sudo` is available (see [[pi-connection]]).
- This removes Wi-Fi as an emergency management path. That's the intended trade for a wired station,
  but say it back to the user once so it's not a surprise if ethernet ever fails.

## 1. Confirm the link is wired (safety gate)

```
ssh <pi> 'ip route get 1.1.1.1 2>/dev/null; ip -br addr show | grep -iE "eth|end0|enp"; \
          echo "active-conn:"; nmcli -t -f DEVICE,TYPE,STATE device status 2>/dev/null | grep -i wifi'
```

Proceed only if the session/route is over an ethernet device. If Wi-Fi is the active carrier, abort.

## 2. Disable in firmware + services (idempotent)

```
ssh <pi> 'bash -s' <<'EOF'
CFG=/boot/firmware/config.txt
grep -qxF 'dtoverlay=disable-wifi' "$CFG" || echo 'dtoverlay=disable-wifi' | sudo tee -a "$CFG"
grep -qxF 'dtoverlay=disable-bt'   "$CFG" || echo 'dtoverlay=disable-bt'   | sudo tee -a "$CFG"
sudo systemctl disable --now bluetooth hciuart 2>/dev/null || true
sudo rfkill block wifi bluetooth 2>/dev/null || true      # quiet immediately, before the reboot
echo "config now:"; grep -nE '^dtoverlay=disable-(wifi|bt)' "$CFG"
EOF
```

`grep -qxF` keeps it safe to run repeatedly — never appends a duplicate line.

## 3. Reboot

```
ssh <pi> 'sudo reboot' ; echo "rebooting — SSH will drop; this is expected"
```

## 4. Wait for the Pi to come back

Poll SSH until it answers again (firmware re-init + boot ≈ 30–60 s). Prefer a background poller so
you don't block. Reconnect at the **same address** you recorded; on the direct link the DHCP lease
(e.g. `192.168.2.2`) returns on the same MAC. If it doesn't come back within ~3 min, report that —
a config.txt typo can stop boot, though `dtoverlay=disable-*` are standard and safe.

## 5. Verify both radios are truly gone

The decisive test for *firmware* disable is that the interfaces **don't exist at all** (not merely
soft-blocked):

```
ssh <pi> 'bash -s' <<'EOF'
echo "wlan0 present? "; ip link show wlan0 >/dev/null 2>&1 && echo "  STILL PRESENT (not fully disabled)" || echo "  gone ✓"
echo "hci/bluetooth present? "; ls /sys/class/bluetooth/ 2>/dev/null | grep -q . && echo "  STILL PRESENT" || echo "  gone ✓"
echo "nmcli wifi:"; nmcli radio wifi 2>/dev/null || echo "  (no wifi device) ✓"
echo "rfkill:"; rfkill list 2>/dev/null || echo "  (no rfkill radios listed) ✓"
echo "bluetooth service:"; systemctl is-active bluetooth 2>/dev/null; systemctl is-enabled bluetooth 2>/dev/null
echo "overlays in config.txt:"; grep -E '^dtoverlay=disable-(wifi|bt)' /boot/firmware/config.txt
echo "dmesg confirmation:"; dmesg 2>/dev/null | grep -iE 'disable-wifi|disable-bt' | tail -2
EOF
```

Interpretation: **`wlan0` gone AND no `/sys/class/bluetooth` entry = success** (radios unpowered at
firmware). If they merely show as soft-blocked in `rfkill` but still exist, the overlays didn't take
— re-check `config.txt` is on the active `/boot/firmware` partition and that the reboot actually
happened.

## 6. Report

State plainly: both radios firmware-disabled and confirmed absent after reboot, the Pi is on wired
ethernet, and Wi-Fi is no longer available as a fallback. Then confirm the station still works —
one `/pi-status`-style check (`/healthz` + both services `active`) so "radio-quiet" didn't take the
services down with it.

## Reversing it (go radio-loud again)

Remove the two `dtoverlay=disable-*` lines from `/boot/firmware/config.txt`, re-enable the services
(`sudo systemctl enable --now bluetooth hciuart`), `sudo rfkill unblock wifi bluetooth`, and reboot.
`wlan0`/`hci0` reappear on the next boot.
