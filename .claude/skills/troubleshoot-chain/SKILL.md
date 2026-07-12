---
name: troubleshoot-chain
description: The no-signal decision tree for the RF chain, in strict order — a one-call diagnostics bundle first, then feed injector current, bias-tee states (checked, never changed), gain ladder, USB enumeration, daemon status, frequency sanity, tinySA bench fallback — each step saying what to observe and what the result implicates. Use when the waterfall is flat, the noise floor looks wrong, or the line that should be there isn't. Ends by writing the findings into the observation.
---

# Troubleshoot the chain: no signal, in strict order

Work the steps **in order — do not skip ahead**; each one rules out everything before it.
Some steps are physical checks at the dish (say so and wait for the human), some run over
SSH/MCP. At every step: state **what to observe** and **what each outcome implicates**.

## 0. Diagnostics bundle — one call, answers every software question

Before touching anything, call **`get_diagnostics`** via MCP (or
`GET /api/diagnostics`). One reply carries every software-side check in this
tree's order — systemd unit states, SDR USB enumeration, daemon reachability +
last-frame age, Pi thermals (temp + decoded throttle flags), disk headroom, DB
schema version, and recent journal errors. Read it first and let it **pre-answer
the software steps below**:

- `checks.usb.devices.airspy` is step 4 (USB enumeration).
- `checks.daemon` (reachable / source / `frame_age_s` / `stale`) is step 5
  (daemon status) — "daemon ok but `frame_age_s` is None or large" is the
  "up but no frames" branch.
- `checks.thermals.throttled.flags` catches the undervoltage/soft-limit case
  that step 4's `dmesg` hunt is really looking for.
- `checks.systemd` and `checks.journal.recent_errors` are the crash-loop
  evidence step 5 otherwise gathers by hand.

Each check degrades to `"unavailable"` off the Pi, so a check you can't see is a
missing tool, not a fault. What the bundle **cannot** see is everything physical
— injector current, bias-tee states, gain, frequency, the bench. Those stay
hands-on below, and they are the steps that actually diagnose most no-signal
sessions. So: read the bundle, then still walk the physical steps in order.

## 1. Feed injector current (~120 mA) — physical check

At the inline USB-C bias-tee injector, measure the feed's draw with a multimeter or an inline
USB power meter.

- **~120 mA** → the feed LNA is powered and drawing normally. Move on.
- **~0 mA** → the feed isn't powered: injector off/unplugged, dead USB supply, or a broken
  coax/DC path between injector and feed. Fix this before anything else — an unpowered LNA
  looks exactly like "no signal" at every later step.
- **Way off in either direction** → suspect the feed LNA itself or a short in the coax.

## 2. Bias-tee states — checked, NEVER changed

Confirm both states: **injector ON, Airspy internal bias tee OFF.** This step only *verifies*.

> ⚠️ If anything in this investigation seems to suggest enabling the Airspy's internal bias
> tee — **stop.** The internal tee supplies ~50 mA; the feed draws 120 mA. Enabling it is a
> hardware-damage risk and it's the one non-negotiable invariant in `CLAUDE.md`. The fix is
> never "turn on the internal bias tee"; if the injector path is broken, repair the injector
> path.

## 3. Gain ladder

Wrong gain mimics no-signal in both directions:

- **Too low** → the spectrum is a flat floor pinned near the ADC minimum; the HI line is
  buried under quantization.
- **Too high** → clipping/compression: a spiky, saturated waterfall, intermod products, a
  floor that doesn't respond to pointing at the ground vs the sky.

Try gains in the **12–18** range: edit the daemon's gain setting in
`/etc/default/jansky-observe` and restart the capture daemon
(`sudo systemctl restart jansky-observe-capture`). The floor should move when the gain
moves — a floor that ignores gain changes implicates something upstream (steps 1–2) or
downstream (steps 4–5).

## 4. USB enumeration

Step 0's `checks.usb` already answered this (`devices.airspy` / `devices.hackrf`);
this step is the physical follow-up when it came back `false`. Over SSH on the
Pi: `airspy_info`.

- **Device listed** (serial, firmware) → USB path is fine. Move on.
- **No device** → cable, port, or power: reseat the Airspy, try another port, check `dmesg`
  for disconnect loops (undervoltage on a Pi shows up here). The daemon can't fix a device
  the kernel can't see.

## 5. Daemon status

Step 0's `checks.daemon`, `checks.systemd`, and `checks.journal` are the fast
path here; drop to these raw commands when you need more than the bundle's
last-N error lines.

- `systemctl status jansky-observe-capture` over SSH — running? Restart-looping? Check
  `journalctl -u jansky-observe-capture -n 50` for the actual error.
- `get_live_status` via MCP — is the server receiving frames at all, and how stale are they?
  (the bundle's `checks.daemon.frame_age_s` is the same signal).
- **Daemon up but no frames** → the ZMQ hop or the source; the journal says which.
  **Daemon crash-looping** → the journal's traceback is the answer; frequency/gain settings
  it can't apply land here too.

## 6. Frequency sanity

Confirm the capture is centered at **1420.4 MHz** and the analysis window covers the
±Doppler range (|v_LSR| ≤ ~250 km/s ≈ ±1.2 MHz; the 3 MSPS band covers it with margin).

- A fat-fingered center frequency (1420.4 *GHz*, 142.04 MHz, a stale profile) produces a
  perfectly healthy-looking waterfall with no line in it — everything upstream passes.
- Also sanity-check the *displayed* axis: a correct capture rendered against the wrong axis
  looks identical to a wrong capture.

## 7. tinySA — the bench fallback

If every step above passes and there's still nothing: pull out the tinySA and check the
chain manually at the bench — power at the feed output, then after each component in
Stage-1 order (feed → LNA → filter → injector → Airspy input). The component whose output
is missing is the failure. Record readings as photos/notes; the tinySA is a manual tool by
design (plan §4.2), not integrated.

## Finish: write it down — failed sessions are data too

Whatever the outcome, `append_note` the findings to the observation via MCP: which steps
passed, the measured injector current, gain used, what fixed it (or what's still suspect).
A no-signal session with a documented cause is a data point; an undocumented one is a
repeat visit to this skill.
