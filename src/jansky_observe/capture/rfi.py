"""RFI view: what a HackRF sweep actually shows, beyond its loudest bin.

:mod:`jansky_observe.capture.hackrf_sweep` captures a sweep and reduces it to a top-5 table.
That is enough to answer "is something screaming at me", and not enough to answer the
questions a site survey is actually for.

**The thing this module exists for: occupancy.**

``summarize_sweep`` averages every bin across every sweep. Averaging is exactly the wrong
reduction for interference, because it destroys the most diagnostic property RFI has — how
*often* it is there.

A bin sitting 10 dB above the floor in 100% of sweeps is a **constant carrier**: a
transmitter, a switching supply, a bad LED driver. It will be there tonight and tomorrow, it
integrates coherently into your baseline, and you fix it by finding it or filtering it.

A bin sitting 10 dB above the floor in 3% of sweeps is **bursty**: a key fob, a doorbell, a
neighbour's microwave, a passing aircraft's transponder. It averages to almost nothing, so
the mean hides it — and then it lands in one integration out of thirty and puts a spike in
your spectrum you will spend an evening explaining.

Both average to a similar number. They need completely different responses. The raw
``hackrf_sweep`` CSV records every sweep separately, so the information is already on disk;
nothing was reading it.

**On band annotations.** Only one allocation here is a global fact: ITU Radio Regulations
footnote **5.340** prohibits *all* emissions in 1400-1427 MHz, which is why the hydrogen line
is observable at all. Everything else is region-specific, changes, and is marked as a hint to
be verified locally rather than as authority. A tool that confidently mislabels a spike is
worse than one that says "unallocated here, go and look".
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

__all__ = [
    "ALLOCATIONS",
    "Allocation",
    "Interferer",
    "PROTECTED_HI_BAND_HZ",
    "ProtectedBandReport",
    "SweepProfile",
    "allocation_for",
    "interferers",
    "protected_band_report",
    "read_sweeps",
    "sweep_profile",
]

#: ITU Radio Regulations footnote 5.340: all emissions prohibited, 1400-1427 MHz. This is the
#: allocation the 21 cm line lives in, and the only one in this module that is a global fact
#: rather than a regional hint.
PROTECTED_HI_BAND_HZ: tuple[float, float] = (1_400e6, 1_427e6)

#: A bin this far above the noise floor counts as "present" in a sweep. 6 dB is a factor of
#: four in power — comfortably above the sweep-to-sweep scatter of a quiet bin, and low enough
#: to catch something you would see in a spectrum.
DEFAULT_THRESHOLD_DB = 6.0

#: Above this fraction of sweeps, an interferer is treated as always-on rather than episodic.
CONSTANT_OCCUPANCY = 0.9

#: Below this, it is bursty: rare enough that the mean hides it and a single integration
#: catches it.
BURSTY_OCCUPANCY = 0.25


@dataclass(frozen=True)
class Allocation:
    """A frequency range and what is usually in it.

    ``authoritative`` separates the one global fact from the regional hints. Only ITU 5.340
    gets it, and the UI must show the difference — labelling a spike "GPS" when it is a
    neighbour's amplifier would send someone hunting the wrong thing.
    """

    lo_hz: float
    hi_hz: float
    label: str
    note: str
    authoritative: bool = False
    source: str = ""

    def contains(self, freq_hz: float) -> bool:
        return self.lo_hz <= freq_hz < self.hi_hz


#: Ordered narrowest-first so :func:`allocation_for` reports the most specific match.
ALLOCATIONS: tuple[Allocation, ...] = (
    Allocation(
        1_400e6,
        1_427e6,
        "protected radio astronomy (ITU 5.340)",
        "All emissions prohibited worldwide. Anything you see in here is either local to your "
        "site, a receiver artefact, or spillover from an adjacent band — and it is worth "
        "finding, because this is the band the hydrogen line lives in.",
        authoritative=True,
        source="ITU Radio Regulations footnote 5.340",
    ),
    Allocation(
        1_574.0e6,
        1_576.8e6,
        "GNSS L1 (hint)",
        "GPS L1 is centred at 1575.42 MHz. A steady peak here is expected and is not your "
        "problem — it is a useful sanity check that the sweep is working at all.",
    ),
    Allocation(
        1_559e6,
        1_610e6,
        "radionavigation-satellite (hint)",
        "GNSS downlinks. Steady, expected, and far enough from 1420 to be harmless unless "
        "your front end is being compressed by it.",
    ),
    Allocation(
        1_616e6,
        1_626.5e6,
        "Iridium (hint)",
        "Bursty by nature — satellites come and go. Expect low occupancy and high peaks, "
        "which is exactly the signature this module separates from a constant carrier.",
    ),
    Allocation(
        1_427e6,
        1_518e6,
        "mobile / fixed (hint, region-specific)",
        "The band immediately above the protected allocation. In many regions this carries "
        "mobile services, and it is the most common source of spillover into 1400-1427 for "
        "a wideband front end. Verify what is licensed where you are.",
    ),
    Allocation(
        1_300e6,
        1_400e6,
        "radiolocation / aeronautical radionavigation (hint)",
        "Air-traffic and weather radar in many regions. Radars are pulsed, so they show as "
        "high peaks at low occupancy, and they can desensitise a front end without ever "
        "appearing in your averaged spectrum.",
    ),
)


def read_sweeps(csv_path: str | Path) -> list[dict[float, float]]:
    """Split a ``hackrf_sweep`` CSV into individual sweeps, preserving each one.

    ``hackrf_sweep`` emits one row per frequency segment and marches upward, so a new sweep
    begins wherever ``hz_low`` stops increasing. Grouping on that gives back the per-sweep
    structure that :func:`~jansky_observe.capture.hackrf_sweep.averaged_bins` collapses.

    Malformed rows are skipped — the raw file stays authoritative, exactly as
    ``averaged_bins`` treats it.
    """
    sweeps: list[dict[float, float]] = []
    current: dict[float, float] = {}
    previous_low = float("inf")
    for line in Path(csv_path).read_text().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 7:
            continue
        try:
            hz_low, width = float(parts[2]), float(parts[4])
            powers = [float(p) for p in parts[6:]]
        except ValueError:
            continue
        if hz_low <= previous_low and current:
            sweeps.append(current)
            current = {}
        previous_low = hz_low
        for index, power in enumerate(powers):
            current[hz_low + (index + 0.5) * width] = power
    if current:
        sweeps.append(current)
    if not sweeps:
        raise ValueError(f"no sweep rows in {csv_path}")
    return sweeps


@dataclass(frozen=True)
class SweepProfile:
    """A sweep reduced without throwing away how often each bin was loud."""

    freq_hz: tuple[float, ...]
    mean_db: tuple[float, ...]
    #: The loudest this bin ever got, across sweeps. A bursty bin's peak is the number that
    #: matters, and its mean is the number that misleads.
    max_db: tuple[float, ...]
    #: Fraction of sweeps in which this bin sat at least ``threshold_db`` above the floor.
    occupancy: tuple[float, ...]
    n_sweeps: int
    #: Robust noise-floor estimate: the median of every bin in every sweep.
    floor_db: float
    threshold_db: float

    @property
    def freq_range_hz(self) -> tuple[float, float]:
        return (self.freq_hz[0], self.freq_hz[-1])

    def nearest(self, freq_hz: float) -> int:
        """Index of the bin closest to a frequency."""
        return min(range(len(self.freq_hz)), key=lambda i: abs(self.freq_hz[i] - freq_hz))


def sweep_profile(
    csv_path: str | Path, *, threshold_db: float = DEFAULT_THRESHOLD_DB
) -> SweepProfile:
    """Reduce a sweep to mean, peak **and occupancy** per bin.

    The noise floor is the median across every bin of every sweep. Median rather than mean
    because a handful of strong carriers drags a mean floor upward and then hides everything
    else beneath it — the failure mode where a site with one loud transmitter reports itself
    as clean.
    """
    sweeps = read_sweeps(csv_path)
    everything = [power for sweep in sweeps.copy() for power in sweep.values()]
    floor = statistics.median(everything)
    limit = floor + threshold_db

    freqs = sorted({freq for sweep in sweeps for freq in sweep})
    means: list[float] = []
    peaks: list[float] = []
    occupancies: list[float] = []
    for freq in freqs:
        seen = [sweep[freq] for sweep in sweeps if freq in sweep]
        means.append(sum(seen) / len(seen))
        peaks.append(max(seen))
        occupancies.append(sum(1 for power in seen if power >= limit) / len(seen))
    return SweepProfile(
        freq_hz=tuple(freqs),
        mean_db=tuple(means),
        max_db=tuple(peaks),
        occupancy=tuple(occupancies),
        n_sweeps=len(sweeps),
        floor_db=floor,
        threshold_db=threshold_db,
    )


@dataclass(frozen=True)
class Interferer:
    """One bin that stood above the floor, and how it behaved."""

    freq_hz: float
    mean_db: float
    max_db: float
    occupancy: float
    #: ``"constant"``, ``"intermittent"`` or ``"bursty"``.
    character: str
    allocation: Allocation | None

    @property
    def above_floor_db(self) -> float:
        """How far the *peak* stood above the noise floor, which is what you would see."""
        return self.max_db

    def advice(self) -> str:
        """What this character actually implies for an observation."""
        if self.character == "constant":
            return (
                "Always on. It will be in every integration, it will not average away, and it "
                "sets a floor you cannot integrate below. Find it or filter it."
            )
        if self.character == "bursty":
            return (
                "Rare and strong. The mean hides it; a single integration catches it. This is "
                "the one that produces an unexplained spike in one scan out of thirty — "
                "record the time, and prefer median over mean when stacking."
            )
        return (
            "Comes and goes. Present often enough to contaminate a fraction of your "
            "integrations — check whether it correlates with time of day before blaming the "
            "sky."
        )


def allocation_for(freq_hz: float) -> Allocation | None:
    """The most specific known allocation containing a frequency, or ``None``.

    ``None`` is a real answer and the honest one for most of the spectrum. Guessing a label
    would send someone hunting the wrong transmitter.
    """
    matches = [band for band in ALLOCATIONS if band.contains(freq_hz)]
    if not matches:
        return None
    return min(matches, key=lambda band: band.hi_hz - band.lo_hz)


def interferers(
    profile: SweepProfile, *, min_occupancy: float = 0.02, top_n: int = 20
) -> list[Interferer]:
    """Bins that stood above the floor, strongest peak first.

    ``min_occupancy`` defaults to 2% rather than 0 so that single-sweep noise excursions do
    not fill the table. Anything genuinely bursty that only ever appeared once in twenty
    sweeps is still caught at the default sweep count.
    """
    found: list[Interferer] = []
    for index, occupancy in enumerate(profile.occupancy):
        if occupancy < min_occupancy:
            continue
        if occupancy >= CONSTANT_OCCUPANCY:
            character = "constant"
        elif occupancy <= BURSTY_OCCUPANCY:
            character = "bursty"
        else:
            character = "intermittent"
        found.append(
            Interferer(
                freq_hz=profile.freq_hz[index],
                mean_db=profile.mean_db[index],
                max_db=profile.max_db[index],
                occupancy=occupancy,
                character=character,
                allocation=allocation_for(profile.freq_hz[index]),
            )
        )
    found.sort(key=lambda item: item.max_db, reverse=True)
    return found[:top_n]


@dataclass(frozen=True)
class ProtectedBandReport:
    """Is 1400-1427 MHz usable at this site? The one verdict that decides the rest."""

    covered: bool
    clean: bool
    worst: Interferer | None
    n_occupied_bins: int
    floor_db: float
    notes: tuple[str, ...] = field(default_factory=tuple)

    def summary(self) -> str:
        if not self.covered:
            return (
                "The sweep did not cover 1400-1427 MHz, so it says nothing about the "
                "protected band. Sweep 1300-1700 MHz to include it."
            )
        if self.clean:
            return (
                "1400-1427 MHz is clean at this site: nothing stood above the noise floor in "
                "the protected band. That is the result you want, and it is worth recording "
                "with a date — it is a property of your neighbourhood, not of your hardware."
            )
        worst = self.worst
        assert worst is not None
        return (
            f"1400-1427 MHz is NOT clean: {self.n_occupied_bins} bin(s) stood above the "
            f"floor. Worst is {worst.freq_hz / 1e6:.3f} MHz at {worst.max_db:.1f} dB "
            f"({worst.occupancy:.0%} of sweeps, {worst.character}). All emissions are "
            "prohibited here under ITU 5.340, so this is local to you — and findable."
        )


def protected_band_report(profile: SweepProfile) -> ProtectedBandReport:
    """Judge the protected band specifically, because it is the question that matters.

    A site survey that reports "the loudest thing is GPS at 1575" has answered a question
    nobody asked. What decides whether hydrogen-line work is viable here is whether anything
    is inside 1400-1427 — where, by treaty, nothing should be.
    """
    lo, hi = PROTECTED_HI_BAND_HZ
    covered = profile.freq_hz[0] <= lo and profile.freq_hz[-1] >= hi
    inside = [
        interferer
        for interferer in interferers(profile, top_n=len(profile.freq_hz))
        if lo <= interferer.freq_hz < hi
    ]
    notes: list[str] = []
    if not covered:
        notes.append(
            f"Swept {profile.freq_hz[0] / 1e6:.0f}-{profile.freq_hz[-1] / 1e6:.0f} MHz, which "
            "does not span 1400-1427 MHz."
        )
    else:
        notes.append(
            "Emissions in 1400-1427 MHz are prohibited worldwide (ITU Radio Regulations "
            "footnote 5.340). Anything here is local: your own equipment, a neighbour's, or "
            "spillover from the band above being let through by a wide front end."
        )
        if any(item.character == "bursty" for item in inside):
            notes.append(
                "At least one interferer here is bursty, which averaging would have hidden. "
                "Note the wall-clock time — intermittent RFI is often diurnal, and knowing "
                "when it appears is most of finding it."
            )
    return ProtectedBandReport(
        covered=covered,
        clean=covered and not inside,
        worst=inside[0] if inside else None,
        n_occupied_bins=len(inside),
        floor_db=profile.floor_db,
        notes=tuple(notes),
    )


def profile_summary(profile: SweepProfile) -> dict[str, object]:
    """JSON-friendly reduction, for the API and the report."""
    report = protected_band_report(profile)
    return {
        "n_sweeps": profile.n_sweeps,
        "freq_range_hz": list(profile.freq_range_hz),
        "floor_db": profile.floor_db,
        "threshold_db": profile.threshold_db,
        "protected_band": {
            "covered": report.covered,
            "clean": report.clean,
            "n_occupied_bins": report.n_occupied_bins,
            "summary": report.summary(),
        },
        "interferers": [
            {
                "freq_hz": item.freq_hz,
                "mean_db": item.mean_db,
                "max_db": item.max_db,
                "occupancy": item.occupancy,
                "character": item.character,
                "allocation": item.allocation.label if item.allocation else None,
            }
            for item in interferers(profile)
        ],
    }


def compare_profiles(before: SweepProfile, after: SweepProfile) -> list[dict[str, object]]:
    """Bins whose **occupancy** changed between two sweeps.

    Complements ``hackrf_sweep.compare_sweeps``, which compares mean power. A carrier that
    switches from occasional to permanent may barely move its mean while changing entirely
    what it does to your data.
    """
    lookup = {freq: index for index, freq in enumerate(before.freq_hz)}
    changes: list[dict[str, object]] = []
    for index, freq in enumerate(after.freq_hz):
        if freq not in lookup:
            continue
        was = before.occupancy[lookup[freq]]
        now = after.occupancy[index]
        if abs(now - was) < 0.25:
            continue
        changes.append(
            {
                "freq_hz": freq,
                "before_occupancy": was,
                "after_occupancy": now,
                "delta": now - was,
                "allocation": (lambda a: a.label if a else None)(allocation_for(freq)),
            }
        )
    changes.sort(key=lambda item: abs(float(item["delta"])), reverse=True)  # type: ignore[arg-type]
    return changes


def describe(profile: SweepProfile, found: Sequence[Interferer] | None = None) -> list[str]:
    """Plain-language lines for the UI, in the order a person would want them."""
    found = interferers(profile) if found is None else found
    lines = [
        f"{profile.n_sweeps} sweeps, {profile.freq_hz[0] / 1e6:.0f}-"
        f"{profile.freq_hz[-1] / 1e6:.0f} MHz, noise floor {profile.floor_db:.1f} dB.",
        protected_band_report(profile).summary(),
    ]
    constant = sum(1 for item in found if item.character == "constant")
    bursty = sum(1 for item in found if item.character == "bursty")
    if found:
        lines.append(
            f"{len(found)} bin(s) above the floor: {constant} constant, {bursty} bursty. "
            "Averaging alone would have shown you the constant ones and hidden the bursty "
            "ones, which are the ones that ruin a single integration."
        )
    else:
        lines.append("Nothing stood above the noise floor anywhere in the swept range.")
    return lines
