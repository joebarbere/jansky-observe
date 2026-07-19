---
name: pi-status
description: Connect to the live Discovery Dish Raspberry Pi and report its status — one-call HTTP diagnostics bundle (systemd units, SDR USB, capture daemon + last-frame age, thermals, disk, DB schema, journal errors) first, then SSH only for what HTTP can't answer, then a short verdict. Use when asked to check on the Pi, whether the station is up, whether the running version matches, or whether the SDR is connected.
---

# Check the Pi's status

The station Pi is the source of truth for "is it up right now" — the repo can't tell you that.
Work outside-in: cheapest reachable check first, SSH only for what HTTP can't answer, then a
one-paragraph verdict. **Read-only** — this skill never restarts a service, changes the source,
or touches the SDR/bias-tee path.

## Connection facts

| What | Value |
|---|---|
| Host | `raspberrypi.local` (default hostname — `install.sh` does **not** rename the Pi) |
| SSH login user | `joe` (key-based; `ssh joe@raspberrypi.local` works passwordless) |
| Service account | `jansky` (runs the units; login is key-only, don't SSH as it) |
| API server | port **8000**, `jansky-observe.service` |
| Capture daemon | `jansky-observe-capture.service` (SDR owner) |
| Install prefix / data | `/opt/jansky-observe` · `/var/lib/jansky-observe` |
| Source config | `/etc/default/jansky-observe` (`JANSKY_OBSERVE_SOURCE=synthetic|hline`) |

If `raspberrypi.local` doesn't resolve, try `dscacheutil -q host -a name raspberrypi.local`, or
ask the user for the current IP — the hostname/IP can change on a re-image or DHCP lease.

## 1. Reachability + version — HTTP, no SSH

```
curl -s -m 6 http://raspberrypi.local:8000/healthz
```

`{"status":"ok","version":"X.Y.Z"}` means the API server is up. **No response / rc≠0** →
either the Pi is down or the server unit is stopped; jump to step 3 (SSH) to tell which.
Compare `version` to the local tip (`git describe --tags` / newest tag) — a mismatch means the
Pi is behind and may want an upgrade (see `/release`), not that anything is broken.

## 2. Full status — the one-call diagnostics bundle

```
curl -s -m 8 http://raspberrypi.local:8000/api/diagnostics | python3 -m json.tool
```

This is the same bundle `/troubleshoot-chain` opens with. One reply answers every software-side
question. Read `checks.*` and report:

- **`systemd`** — both `jansky-observe.service` and `jansky-observe-capture.service` should be
  `active` + `enabled`. Anything else is the headline.
- **`daemon`** — `reachable`, `source` (`synthetic` vs `hline`), `capturing`, `fps`,
  `frame_age_s`. A stale `frame_age_s` (seconds climbing) on a supposedly-live daemon is a
  quiet failure worth flagging even when systemd says `active`.
- **`usb`** — `airspy`/`hackrf` booleans. `airspy:false` is **`warn`, not error, when
  `source=synthetic`** — expected with no SDR plugged in. It's only a real problem when the
  source is `hline` (real capture with no radio present).
- **`thermals`** — `temp_c` and decoded `throttled` flags (`null` off a real Pi/QEMU). Flag any
  active throttle.
- **`disk`** — `free_gb` / `fraction_free`. Call out < ~10% free.
- **`database`** — `user_version` vs `expected`; they should match. A gap means a pending
  migration (the server migrates forward on start).
- **`journal`** — `recent_errors` per unit; empty lists are good, quote anything present.

## 3. Only what HTTP can't answer — SSH

Reach for SSH when `/healthz` is dead (is it the Pi or just the unit?), or you need uptime, the
configured source, or raw logs. Prefer one batched call:

```
ssh -o ConnectTimeout=8 -o BatchMode=yes joe@raspberrypi.local 'bash -s' <<'EOF'
uptime
systemctl is-active jansky-observe.service jansky-observe-capture.service
grep -vE "^\s*#|^\s*$" /etc/default/jansky-observe
journalctl -u jansky-observe-capture.service -n 8 --no-pager
EOF
```

If the host pings but `/healthz` is dead and `systemctl is-active` shows the unit stopped, the
Pi is fine and only the service is down — say exactly that; do **not** restart it unless the user
asks.

## 4. Verdict

Close with a compact table (unit states, version, source, SDR, disk, DB) and one plain-English
line: healthy, healthy-but-idle-on-synthetic, or the specific thing that's wrong. If the source
is `synthetic` and the user expected real sky, name it — that's the most common "why is nothing
happening" cause, and switching it (edit `/etc/default/jansky-observe`, restart the capture unit)
is a change to offer, not to make unasked.
