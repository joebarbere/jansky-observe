"""A stand-in for ``hackrf_sweep`` used by tests — no hardware, deterministic CSV.

Accepts the flags :func:`build_sweep_cmd` emits (``-f lo:hi`` MHz, ``-w`` bin
width Hz, ``-N`` sweeps) and prints hackrf_sweep-style CSV rows to stdout,
5 bins per row. Powers are deterministic so summaries are assertable: with
``n_bins`` total bins, the bin at index ``n_bins // 4`` is -38.0 dB, the bin
at ``n_bins // 2`` is -45.0 dB, and every other bin alternates -70/-69 dB by
sweep parity (so cross-sweep averaging of an even sweep count gives -69.5).
If ``-p`` appears in argv it exits 86 loudly: the antenna-power (bias-tee)
flag must never reach a real hackrf_sweep.
"""

from __future__ import annotations

import sys

NOISE_DB = -70.0
PEAK1_DB = -38.0
PEAK2_DB = -45.0
BINS_PER_ROW = 5


def _opt(flag: str) -> str:
    return sys.argv[sys.argv.index(flag) + 1]


def main() -> int:
    if "-p" in sys.argv:
        print("FATAL: antenna-power flag passed to fake hackrf_sweep", file=sys.stderr)
        return 86
    lo_mhz, hi_mhz = (int(part) for part in _opt("-f").split(":"))
    width = int(_opt("-w"))
    sweeps = int(_opt("-N"))
    lo_hz = lo_mhz * 1_000_000
    hi_hz = hi_mhz * 1_000_000
    n_bins = (hi_hz - lo_hz) // width
    peak1, peak2 = n_bins // 4, n_bins // 2
    for sweep in range(sweeps):
        noise = NOISE_DB + (sweep % 2)
        for row_start in range(0, n_bins, BINS_PER_ROW):
            row_bins = min(BINS_PER_ROW, n_bins - row_start)
            row_lo = lo_hz + row_start * width
            row_hi = row_lo + row_bins * width
            powers = []
            for b in range(row_start, row_start + row_bins):
                db = PEAK1_DB if b == peak1 else PEAK2_DB if b == peak2 else noise
                powers.append(f"{db:.2f}")
            print(
                f"2026-01-01, 00:00:{sweep % 60:02d}.000000, {row_lo}, {row_hi}, "
                f"{width:.2f}, 20, " + ", ".join(powers)
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
