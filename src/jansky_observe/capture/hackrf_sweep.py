"""HackRF RFI-survey mode via a ``hackrf_sweep`` subprocess (plan §4.2).

``hackrf_sweep`` sweeps a frequency range (up to 8 GHz/s) and prints binned
power to stdout as CSV rows::

    date, time, hz_low, hz_high, hz_bin_width, num_samples, dB, dB, ...

:func:`run_sweep` persists that stdout verbatim to
``<data_dir>/captures/rfi-<utcstamp>Z.csv`` — the raw CSV *is* the capture.
:func:`summarize_sweep` reduces it to the loudest bins averaged across sweeps
for the "what does my window pass-through actually see?" pre-session survey.

**The bias-tee rule** (CLAUDE.md safety invariants): ``hackrf_sweep`` has a
``-p`` antenna-port-power flag. Same rule as the Airspy's ``-b``:
:func:`build_sweep_cmd` has *no parameter* that could emit it — the H-line
feed is powered by the inline USB-C injector, never by SDR port power. Do
not add one.
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

__all__ = [
    "DEFAULT_BIN_WIDTH_HZ",
    "DEFAULT_NUM_SWEEPS",
    "averaged_bins",
    "build_sweep_cmd",
    "compare_sweeps",
    "rfi_sweep_comparison",
    "run_sweep",
    "summarize_sweep",
]

DEFAULT_BIN_WIDTH_HZ = 1_000_000
DEFAULT_NUM_SWEEPS = 20
SWEEP_TIMEOUT_S = 60.0
"""Subprocess wall-clock bound — at 8 GHz/s, 20 sweeps of 1 GHz finish in seconds."""


def build_sweep_cmd(
    freq_lo_mhz: float = 1000,
    freq_hi_mhz: float = 2000,
    *,
    bin_width_hz: int = DEFAULT_BIN_WIDTH_HZ,
    num_sweeps: int = DEFAULT_NUM_SWEEPS,
    binary: str | list[str] = "hackrf_sweep",
) -> list[str]:
    """Build the ``hackrf_sweep`` command line.

    ``-f min:max`` is the sweep range in whole MHz, ``-w`` the FFT bin width
    in Hz, ``-N`` the number of sweeps before exit (one-shot survey, not the
    endless default). SAFETY: there is deliberately no way to emit the ``-p``
    antenna-power flag — see the module docstring.
    """
    lo, hi = int(freq_lo_mhz), int(freq_hi_mhz)
    if not 0 <= lo < hi <= 7250:
        raise ValueError(f"need 0 <= freq_lo < freq_hi <= 7250 MHz, got {lo}:{hi}")
    if bin_width_hz <= 0:
        raise ValueError(f"bin_width_hz must be positive, got {bin_width_hz}")
    if num_sweeps < 1:
        raise ValueError(f"num_sweeps must be >= 1, got {num_sweeps}")
    argv = [binary] if isinstance(binary, str) else list(binary)
    return [
        *argv,
        "-f",
        f"{lo}:{hi}",
        "-w",
        str(bin_width_hz),
        "-N",
        str(num_sweeps),
    ]


def run_sweep(
    data_dir: str | Path,
    freq_lo_mhz: float = 1000,
    freq_hi_mhz: float = 2000,
    *,
    bin_width_hz: int = DEFAULT_BIN_WIDTH_HZ,
    num_sweeps: int = DEFAULT_NUM_SWEEPS,
    binary: str | list[str] = "hackrf_sweep",
    timeout_s: float = SWEEP_TIMEOUT_S,
) -> Path:
    """Run one RFI survey; return the path of the persisted CSV capture.

    Blocks for the sweep's duration (seconds). The subprocess's stdout is
    written verbatim to ``<data_dir>/captures/rfi-<utcstamp>Z.csv``. Raises
    :class:`RuntimeError` on a missing binary (naming the ``hackrf`` apt
    package), a non-zero exit, or a timeout; the partial file is removed.
    """
    cmd = build_sweep_cmd(
        freq_lo_mhz,
        freq_hi_mhz,
        bin_width_hz=bin_width_hz,
        num_sweeps=num_sweeps,
        binary=binary,
    )
    captures = Path(data_dir) / "captures"
    captures.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%SZ")
    path = captures / f"rfi-{stamp}.csv"
    try:
        with path.open("wb") as out:
            proc = subprocess.run(cmd, stdout=out, stderr=subprocess.PIPE, timeout=timeout_s)
    except FileNotFoundError:
        path.unlink(missing_ok=True)
        raise RuntimeError(
            f"hackrf_sweep not found ({cmd[0]!r}) — install the 'hackrf' apt package"
        ) from None
    except subprocess.TimeoutExpired:
        path.unlink(missing_ok=True)
        raise RuntimeError(f"hackrf_sweep timed out after {timeout_s:g} s") from None
    if proc.returncode != 0:
        detail = proc.stderr.decode(errors="replace").strip()[:200]
        path.unlink(missing_ok=True)
        raise RuntimeError(f"hackrf_sweep exited with code {proc.returncode}: {detail}")
    return path


def averaged_bins(csv_path: str | Path) -> tuple[dict[float, float], tuple[float, float], int]:
    """Average a ``hackrf_sweep`` CSV to ``{bin_center_hz: mean_power_db}``.

    Malformed rows are skipped (the raw file stays authoritative); a CSV with no
    usable rows raises :class:`ValueError`. Returns the per-bin means, the
    ``(lo, hi)`` frequency range covered, and the usable row count.
    """
    sums: dict[float, list[float]] = {}
    n_rows = 0
    range_lo = float("inf")
    range_hi = float("-inf")
    for line in Path(csv_path).read_text().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 7:
            continue
        try:
            hz_low, hz_high, width = float(parts[2]), float(parts[3]), float(parts[4])
            powers = [float(p) for p in parts[6:]]
        except ValueError:
            continue
        n_rows += 1
        range_lo = min(range_lo, hz_low)
        range_hi = max(range_hi, hz_high)
        for i, power in enumerate(powers):
            acc = sums.setdefault(hz_low + (i + 0.5) * width, [0.0, 0.0])
            acc[0] += power
            acc[1] += 1.0
    if n_rows == 0:
        raise ValueError(f"no sweep rows in {csv_path}")
    means = {freq: total / count for freq, (total, count) in sums.items()}
    return means, (range_lo, range_hi), n_rows


def summarize_sweep(csv_path: str | Path, top_n: int = 5) -> dict[str, Any]:
    """Reduce a ``hackrf_sweep`` CSV capture to its loudest bins.

    Powers are averaged per bin-center frequency across all sweeps, then the
    ``top_n`` loudest are returned, loudest first.

    Returns
    -------
    dict
        ``{"n_rows": int, "freq_range_hz": [lo, hi],
        "loudest": [{"freq_hz": float, "power_db": float}, ...]}``.
    """
    means, (range_lo, range_hi), n_rows = averaged_bins(csv_path)
    ranked = sorted(means.items(), key=lambda item: item[1], reverse=True)
    return {
        "n_rows": n_rows,
        "freq_range_hz": [range_lo, range_hi],
        "loudest": [{"freq_hz": freq, "power_db": power} for freq, power in ranked[:top_n]],
    }


def compare_sweeps(
    before_csv: str | Path,
    after_csv: str | Path,
    *,
    rise_db: float = 6.0,
    top_n: int = 5,
) -> dict[str, Any]:
    """Compare two RFI sweeps bin-for-bin (roadmap M6: the before/after summary).

    Both sweeps should share identical HackRF settings, so their bin centers
    align exactly. Returns the bins that grew by at least ``rise_db`` (new or
    louder interferers), loudest-delta first, plus each sweep's loudest bins for
    context.

    Returns
    -------
    dict
        ``{"rise_db", "n_risen", "risen": [{"freq_hz", "before_db", "after_db",
        "delta_db"}...], "before_loudest": [...], "after_loudest": [...]}``.
    """
    before, _, _ = averaged_bins(before_csv)
    after, _, _ = averaged_bins(after_csv)
    risen: list[dict[str, float]] = []
    for freq, after_db in after.items():
        before_db = before.get(freq)
        if before_db is None:
            continue  # bins only align when settings match; unmatched bins skipped
        delta = after_db - before_db
        if delta >= rise_db:
            risen.append(
                {
                    "freq_hz": freq,
                    "before_db": before_db,
                    "after_db": after_db,
                    "delta_db": delta,
                }
            )
    risen.sort(key=lambda item: item["delta_db"], reverse=True)

    def _loudest(bins: dict[float, float]) -> list[dict[str, float]]:
        ranked = sorted(bins.items(), key=lambda item: item[1], reverse=True)
        return [{"freq_hz": f, "power_db": p} for f, p in ranked[:top_n]]

    return {
        "rise_db": rise_db,
        "n_risen": len(risen),
        "risen": risen[:top_n],
        "before_loudest": _loudest(before),
        "after_loudest": _loudest(after),
    }


def rfi_sweep_comparison(captures: Sequence[Any]) -> dict[str, Any] | None:
    """Before/after summary for an observation's HackRF sweeps (roadmap M6).

    Takes the first and last non-purged ``hackrf_sweep_csv`` captures as the
    before/after pair and runs :func:`compare_sweeps` over their CSVs. Returns
    ``None`` when there are fewer than two, or the files are gone/unreadable.
    Duck-typed on ``format``/``path``/``purged_at``/``id`` so it serves both the
    live detail page and the report without importing the ORM model here.
    """
    sweeps = [
        c
        for c in captures
        if getattr(c, "format", None) == "hackrf_sweep_csv"
        and getattr(c, "purged_at", None) is None
    ]
    if len(sweeps) < 2:
        return None
    before, after = sweeps[0], sweeps[-1]
    try:
        summary = compare_sweeps(before.path, after.path)
    except (OSError, ValueError):
        return None
    summary["before_id"] = before.id
    summary["after_id"] = after.id
    return summary
