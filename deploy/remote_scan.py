#!/usr/bin/env python3
"""Remote RFI scanner client (plans/remote-scanner.md) — runs on the laptop, not the Pi.

Stdlib only, no install: plug a HackRF into the laptop, then

    python3 deploy/remote_scan.py                 # discover station, sweep every 10 min
    python3 deploy/remote_scan.py --once          # one sweep and exit
    python3 deploy/remote_scan.py --dry-run       # wiring check, no HackRF needed
    python3 deploy/remote_scan.py --station http://10.3.1.106:8000

The station keeps observing HI the whole time: uploads go over HTTP to
``POST /api/remote_scan/sweep`` and never touch the capture daemon (unlike
the station-local ``POST /api/rfi_sweep``, which pauses the live stream).
Between sweeps the client heartbeats every 15 s so the station cockpit's
``scanner`` chip shows the connection live.

Station resolution order:
  1. ``--station URL``
  2. ``$JANSKY_STATION_URL``
  3. mDNS browse for ``_jansky-observe._tcp`` (``dns-sd`` on macOS,
     ``avahi-browse`` on Linux — both optional; a few seconds' timeout)
  4. ``http://raspberrypi.local:8000``

SAFETY — the bias-tee rule (jansky-observe CLAUDE.md): ``hackrf_sweep`` has a
``-p`` antenna-port-power flag. :func:`build_cmd` mirrors the server-side
``build_sweep_cmd`` contract: it has *no parameter* that could emit ``-p``,
and :func:`main` refuses ``-p`` smuggled through ``--sweep-arg``. The H-line
feed is powered by its inline injector, never by SDR port power.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_FALLBACK = "http://raspberrypi.local:8000"
HEARTBEAT_S = 15.0
SWEEP_TIMEOUT_S = 300
MDNS_SERVICE = "_jansky-observe._tcp"

FORBIDDEN_SWEEP_ARGS = ("-p",)
"""Antenna port power. Never passed, never accepted. See module docstring."""


def build_cmd(
    freq_lo_mhz: int,
    freq_hi_mhz: int,
    bin_width_hz: int,
    num_sweeps: int,
    extra: list[str] | None = None,
) -> list[str]:
    """The ``hackrf_sweep`` command line. Structurally no-port-power: there is
    no parameter of this function that can emit ``-p``, and ``extra`` is
    screened by the caller (:func:`main`) before it reaches here."""
    cmd = [
        "hackrf_sweep",
        "-f",
        f"{freq_lo_mhz}:{freq_hi_mhz}",
        "-w",
        str(bin_width_hz),
        "-N",
        str(num_sweeps),
    ]
    return cmd + list(extra or [])


def _healthz(base_url: str, timeout_s: float = 3.0) -> bool:
    try:
        with urllib.request.urlopen(f"{base_url}/healthz", timeout=timeout_s) as fh:
            return bool(fh.status == 200)
    except (urllib.error.URLError, OSError, ValueError):
        return False


def _mdns_browse(timeout_s: float = 4.0) -> str | None:
    """Best-effort mDNS browse via the platform CLI; ``None`` when unavailable."""
    if sys.platform == "darwin":
        # dns-sd runs until killed; -B lists instances, then resolve via -L would
        # take a second round-trip. Browsing instance names then trying
        # <instance>.local:8000 covers the common single-station LAN.
        cmd = ["dns-sd", "-B", MDNS_SERVICE, "local."]
    else:
        cmd = ["avahi-browse", "-t", "-p", f"{MDNS_SERVICE}"]
    try:
        proc = subprocess.run(  # noqa: S603 — fixed argv, no shell
            cmd, capture_output=True, text=True, timeout=timeout_s, check=False
        )
        out = proc.stdout
    except FileNotFoundError:
        return None
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
    for line in out.splitlines():
        m = re.search(rf"(\S+)\s+{re.escape(MDNS_SERVICE)}", line) or re.search(
            r"=;.*;(\S+);" if ";" in line else r"$^", line
        )
        if m:
            host = m.group(1).strip().rstrip(".")
            if host and not host.startswith(("+", "=")):
                candidate = f"http://{host}.local:8000"
                if _healthz(candidate):
                    return candidate
    return None


def resolve_station(explicit: str | None) -> str:
    """Apply the resolution order from the module docstring; exits when nothing answers."""
    candidates: list[tuple[str, str]] = []
    if explicit:
        candidates.append(("--station", explicit.rstrip("/")))
    env = os.environ.get("JANSKY_STATION_URL")
    if env:
        candidates.append(("$JANSKY_STATION_URL", env.rstrip("/")))
    for origin, url in candidates:
        if _healthz(url):
            print(f"station: {url} (via {origin})")
            return url
        print(f"station candidate {url} (via {origin}) not answering /healthz", file=sys.stderr)
    discovered = _mdns_browse()
    if discovered:
        print(f"station: {discovered} (via mDNS)")
        return discovered
    if _healthz(DEFAULT_FALLBACK):
        print(f"station: {DEFAULT_FALLBACK} (fallback)")
        return DEFAULT_FALLBACK
    sys.exit(
        "no station found: tried --station/$JANSKY_STATION_URL, mDNS browse, "
        f"and {DEFAULT_FALLBACK}. Is the Pi on this LAN?"
    )


def _post(url: str, data: bytes, content_type: str, timeout_s: float = 30.0) -> dict:
    req = urllib.request.Request(url, data=data, headers={"Content-Type": content_type})
    with urllib.request.urlopen(req, timeout=timeout_s) as fh:
        parsed = json.loads(fh.read().decode())
        return dict(parsed) if isinstance(parsed, dict) else {}


def heartbeat(base_url: str, host: str, note: str | None) -> bool:
    try:
        body = json.dumps({"host": host, "note": note}).encode()
        _post(f"{base_url}/api/remote_scan/heartbeat", body, "application/json", timeout_s=5)
        return True
    except (urllib.error.URLError, OSError, ValueError):
        return False


def run_sweep_and_upload(
    base_url: str,
    host: str,
    args: argparse.Namespace,
    extra: list[str],
) -> dict:
    """One ``hackrf_sweep`` run, streamed to a byte buffer, POSTed as the capture."""
    cmd = build_cmd(args.freq_lo_mhz, args.freq_hi_mhz, args.bin_width_hz, args.num_sweeps, extra)
    print("sweep:", " ".join(cmd))
    proc = subprocess.run(  # noqa: S603 — argv screened in main()
        cmd, capture_output=True, timeout=SWEEP_TIMEOUT_S, check=False
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        raise RuntimeError(
            f"hackrf_sweep failed (rc={proc.returncode}): {proc.stderr.decode()[-300:]}"
        )
    query = urllib.parse.urlencode(
        {
            key: value
            for key, value in {
                "host": host,
                "antenna": args.antenna,
                "label": args.label,
                "num_sweeps": args.num_sweeps,
            }.items()
            if value is not None
        }
    )
    return _post(f"{base_url}/api/remote_scan/sweep?{query}", proc.stdout, "text/csv", 60)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--station", help="station base URL (else env/mDNS/fallback)")
    ap.add_argument("--host", default=socket.gethostname().split(".")[0])
    ap.add_argument("--antenna", help="what is on the HackRF, e.g. 'SRH777CA whip'")
    ap.add_argument("--label", help="free-text location note, e.g. 'patio, toward tower'")
    ap.add_argument("--freq-lo-mhz", type=int, default=1)
    ap.add_argument("--freq-hi-mhz", type=int, default=6000)
    ap.add_argument("--bin-width-hz", type=int, default=1_000_000)
    ap.add_argument("--num-sweeps", type=int, default=20)
    ap.add_argument("--interval", type=float, default=10.0, help="minutes between sweeps")
    ap.add_argument("--once", action="store_true", help="one sweep, then exit")
    ap.add_argument("--dry-run", action="store_true", help="resolve + heartbeat only")
    ap.add_argument(
        "--sweep-arg",
        action="append",
        default=[],
        help="extra hackrf_sweep flag (repeatable); -p is refused",
    )
    args = ap.parse_args(argv)

    for extra_arg in args.sweep_arg:
        if extra_arg.strip() in FORBIDDEN_SWEEP_ARGS or extra_arg.strip().startswith("-p"):
            sys.exit(
                "refusing antenna-port power (-p): the H-line feed is powered by its "
                "inline injector, never by SDR port power (the bias-tee rule)."
            )

    base_url = resolve_station(args.station)
    if not heartbeat(base_url, args.host, args.label):
        print("warning: heartbeat failed (station reachable but API refused?)", file=sys.stderr)
    else:
        print(f"connected as '{args.host}' — the station cockpit now shows this scanner")
    if args.dry_run:
        return 0

    last_sweep = 0.0
    while True:
        now = time.time()
        if now - last_sweep >= args.interval * 60 or last_sweep == 0.0:
            try:
                reply = run_sweep_and_upload(base_url, args.host, args, args.sweep_arg)
                print(
                    f"uploaded capture_id={reply.get('capture_id')} "
                    f"rows={reply.get('n_rows')} -> {reply.get('path')}"
                )
            except (RuntimeError, urllib.error.URLError, OSError, ValueError) as exc:
                print(f"sweep/upload failed: {exc}", file=sys.stderr)
            last_sweep = time.time()
            if args.once:
                return 0
        heartbeat(base_url, args.host, args.label)
        time.sleep(HEARTBEAT_S)


if __name__ == "__main__":
    raise SystemExit(main())
