"""Tests for the RFI view: occupancy, band annotation, and the protected-band verdict.

The point of this module is that ``summarize_sweep`` averages, and averaging destroys the
most diagnostic property interference has — how *often* it is there. So the central test is
not that a number is computed, but that **a bursty interferer the averaged summary misses is
found**, and that a constant one is told apart from it.
"""

from __future__ import annotations

import random

import pytest

from jansky_observe.capture import hackrf_sweep, rfi

BIN_HZ = 1e6
SEGMENT_HZ = 20e6


def synthetic_sweep(
    path,
    *,
    lo_hz: float = 1_300e6,
    hi_hz: float = 1_700e6,
    n_sweeps: int = 20,
    constant: tuple[float, float] | None = (1_452e6, 22.0),
    bursty: tuple[float, float, int] | None = (1_622e6, 28.0, 7),
    in_protected: tuple[float, float, int] | None = (1_409e6, 18.0, 10),
    gnss: tuple[float, float, float] | None = (1_574e6, 1_577e6, 35.0),
    seed: int = 7,
) -> None:
    """Write a hackrf_sweep-format CSV with known constant and bursty interferers.

    ``bursty`` and ``in_protected`` are ``(freq, excess_db, every_n_sweeps)`` — present in one
    sweep out of ``every_n_sweeps``, which is exactly the pattern a mean hides.

    ``gnss`` is a wide, strong, always-on block standing in for GPS L1. It is here because
    without it the fixture is *unrealistically empty*: a single one-bin interferer reaches a
    top-5 by mean simply for lack of competition, and the test that averaging hides bursty
    signals then fails for the wrong reason. Every real L-band sweep has GNSS in it.
    """
    rng = random.Random(seed)
    rows = []
    for sweep in range(n_sweeps):
        freq = lo_hz
        while freq < hi_hz:
            powers = []
            for index in range(int(SEGMENT_HZ / BIN_HZ)):
                centre = freq + (index + 0.5) * BIN_HZ
                power = -80.0 + rng.gauss(0, 1.5)
                if constant and constant[0] <= centre < constant[0] + BIN_HZ:
                    power += constant[1]
                if gnss and gnss[0] <= centre < gnss[1]:
                    power += gnss[2]
                if bursty and bursty[0] <= centre < bursty[0] + BIN_HZ and sweep % bursty[2] == 0:
                    power += bursty[1]
                if (
                    in_protected
                    and in_protected[0] <= centre < in_protected[0] + BIN_HZ
                    and sweep % in_protected[2] == 0
                ):
                    power += in_protected[1]
                powers.append(power)
            rows.append(
                f"2026-08-09, 21:00:0{sweep % 10}, {int(freq)}, {int(freq + SEGMENT_HZ)}, "
                f"{int(BIN_HZ)}, 8192, " + ", ".join(f"{p:.2f}" for p in powers)
            )
            freq += SEGMENT_HZ
    path.write_text("\n".join(rows) + "\n")


@pytest.fixture()
def sweep_csv(tmp_path):
    path = tmp_path / "rfi-test.csv"
    synthetic_sweep(path)
    return path


# --------------------------------------------------------------------------------------
# The reason this module exists
# --------------------------------------------------------------------------------------


def test_occupancy_finds_what_averaging_hides(sweep_csv):
    """A bursty interferer inside the protected band, invisible to the existing summary.

    It is present in 1 sweep out of 10, so its mean sits at the noise floor and it ranks
    nowhere by mean power — while its *peak* stands ~18 dB up, in a band where ITU 5.340
    permits no emissions at all.
    """
    old = hackrf_sweep.summarize_sweep(sweep_csv, top_n=5)
    assert not any(abs(b["freq_hz"] - 1_409.5e6) < BIN_HZ for b in old["loudest"]), (
        "the averaged summary was not supposed to find this — if it does, the fixture is wrong"
    )

    profile = rfi.sweep_profile(sweep_csv)
    index = profile.nearest(1_409.5e6)
    assert profile.occupancy[index] == pytest.approx(0.1, abs=0.05)
    assert profile.max_db[index] - profile.floor_db > 15
    assert profile.mean_db[index] - profile.floor_db < 5  # which is why the mean misses it

    found = [i for i in rfi.interferers(profile) if abs(i.freq_hz - 1_409.5e6) < BIN_HZ]
    assert found, "the occupancy-aware reduction must find it"
    assert found[0].character == "bursty"


def test_constant_and_bursty_are_told_apart(sweep_csv):
    profile = rfi.sweep_profile(sweep_csv)
    by_freq = {round(i.freq_hz / 1e6): i for i in rfi.interferers(profile)}
    assert by_freq[1452].character == "constant"
    assert by_freq[1452].occupancy == 1.0
    assert by_freq[1622].character == "bursty"
    assert by_freq[1622].occupancy < 0.25
    # The distinction is actionable, and the advice differs.
    assert "Always on" in by_freq[1452].advice()
    assert "single integration catches it" in by_freq[1622].advice()


def test_the_mean_to_peak_gap_is_the_intermittency(sweep_csv):
    """A constant carrier's mean and peak nearly coincide; a bursty one's do not."""
    profile = rfi.sweep_profile(sweep_csv)
    constant = profile.nearest(1_452.5e6)
    bursty = profile.nearest(1_622.5e6)
    assert profile.max_db[constant] - profile.mean_db[constant] < 6
    assert profile.max_db[bursty] - profile.mean_db[bursty] > 15


# --------------------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------------------


def test_sweeps_are_split_on_the_frequency_wrap(sweep_csv):
    """hackrf_sweep marches upward and restarts; that restart is the sweep boundary."""
    sweeps = rfi.read_sweeps(sweep_csv)
    assert len(sweeps) == 20
    assert all(len(sweep) == 400 for sweep in sweeps)


def test_malformed_rows_are_skipped_not_fatal(tmp_path):
    path = tmp_path / "messy.csv"
    synthetic_sweep(path)
    path.write_text("garbage\n,,,\n" + path.read_text() + "\nnot, a, row\n")
    profile = rfi.sweep_profile(path)
    assert profile.n_sweeps == 20


def test_an_empty_file_is_an_error_not_an_empty_profile(tmp_path):
    path = tmp_path / "empty.csv"
    path.write_text("nothing here\n")
    with pytest.raises(ValueError, match="no sweep rows"):
        rfi.sweep_profile(path)


def test_the_floor_is_a_median_so_one_loud_carrier_cannot_hide_the_rest(tmp_path):
    """A mean floor is dragged up by strong carriers, which then hides everything else."""
    path = tmp_path / "loud.csv"
    synthetic_sweep(path, constant=(1_452e6, 60.0))  # a very loud carrier
    profile = rfi.sweep_profile(path)
    assert profile.floor_db == pytest.approx(-80, abs=2)
    # The quiet bursty interferer is still found despite the loud neighbour.
    assert any(abs(i.freq_hz - 1_622.5e6) < BIN_HZ for i in rfi.interferers(profile))


# --------------------------------------------------------------------------------------
# Band annotation — only one allocation is a global fact
# --------------------------------------------------------------------------------------


def test_the_protected_band_is_the_only_authoritative_allocation():
    """Regional allocations change; ITU 5.340 does not. The UI must show the difference."""
    authoritative = [band for band in rfi.ALLOCATIONS if band.authoritative]
    assert len(authoritative) == 1
    assert authoritative[0].lo_hz == 1_400e6
    assert authoritative[0].hi_hz == 1_427e6
    assert "5.340" in authoritative[0].source
    assert all("hint" in band.label for band in rfi.ALLOCATIONS if not band.authoritative)


def test_allocation_lookup_prefers_the_most_specific_band():
    """1575.42 is inside both the GNSS L1 window and the wider RNSS allocation."""
    assert rfi.allocation_for(1_575.42e6).label.startswith("GNSS L1")
    assert rfi.allocation_for(1_420.4e6).authoritative
    assert rfi.allocation_for(1_600e6).label.startswith("radionavigation")


def test_an_unknown_frequency_returns_none_rather_than_a_guess():
    """Guessing a label sends someone hunting the wrong transmitter."""
    assert rfi.allocation_for(900e6) is None
    assert rfi.allocation_for(2_400e6) is None


# --------------------------------------------------------------------------------------
# The verdict that decides whether the site is usable
# --------------------------------------------------------------------------------------


def test_the_protected_band_verdict_is_the_headline(sweep_csv):
    report = rfi.protected_band_report(rfi.sweep_profile(sweep_csv))
    assert report.covered
    assert not report.clean
    assert report.worst is not None
    assert "NOT clean" in report.summary()
    assert "5.340" in report.summary()
    assert any("local" in note for note in report.notes)
    assert any("bursty" in note and "diurnal" in note for note in report.notes)


def test_a_clean_protected_band_says_so_and_says_it_is_worth_recording(tmp_path):
    path = tmp_path / "clean.csv"
    synthetic_sweep(path, in_protected=None)
    report = rfi.protected_band_report(rfi.sweep_profile(path))
    assert report.clean
    assert "is clean" in report.summary()
    assert "property of your neighbourhood" in report.summary()


def test_a_sweep_that_misses_the_band_says_nothing_about_it(tmp_path):
    """The honest failure: silence about an unswept band, not a clean verdict."""
    path = tmp_path / "narrow.csv"
    synthetic_sweep(path, lo_hz=1_500e6, hi_hz=1_700e6, in_protected=None)
    report = rfi.protected_band_report(rfi.sweep_profile(path))
    assert not report.covered
    assert not report.clean  # absence of evidence is not a clean bill of health
    assert "did not cover" in report.summary()


# --------------------------------------------------------------------------------------
# Comparison and summary shapes
# --------------------------------------------------------------------------------------


def test_occupancy_comparison_catches_a_carrier_that_became_permanent(tmp_path):
    """A bin going from occasional to always-on barely moves its mean but changes everything."""
    before, after = tmp_path / "before.csv", tmp_path / "after.csv"
    synthetic_sweep(before, constant=None, bursty=(1_452e6, 22.0, 10), in_protected=None)
    synthetic_sweep(after, constant=(1_452e6, 22.0), bursty=None, in_protected=None)
    changes = rfi.compare_profiles(rfi.sweep_profile(before), rfi.sweep_profile(after))
    assert changes
    assert abs(changes[0]["freq_hz"] - 1_452.5e6) < BIN_HZ
    assert changes[0]["delta"] > 0.5


def test_profile_summary_is_json_friendly(sweep_csv):
    import json

    summary = rfi.profile_summary(rfi.sweep_profile(sweep_csv))
    json.dumps(summary)  # must not raise
    assert summary["n_sweeps"] == 20
    assert summary["protected_band"]["covered"] is True
    assert any(i["character"] == "bursty" for i in summary["interferers"])


def test_describe_leads_with_the_protected_band(sweep_csv):
    lines = rfi.describe(rfi.sweep_profile(sweep_csv))
    assert "1400-1427" in lines[1]
    assert any("Averaging alone" in line for line in lines)


# --------------------------------------------------------------------------------------
# The routes
# --------------------------------------------------------------------------------------


@pytest.fixture()
def rfi_client(tmp_path):
    from fastapi.testclient import TestClient
    from sqlmodel import Session

    from jansky_observe.config import Settings
    from jansky_observe.db import init_db
    from jansky_observe.models import Capture
    from jansky_observe.server.app import create_app

    engine = init_db(tmp_path)
    csv = tmp_path / "captures" / "rfi.csv"
    csv.parent.mkdir(parents=True, exist_ok=True)
    synthetic_sweep(csv)
    with Session(engine) as session:
        capture = Capture(
            device="hackrf",
            path=str(csv),
            format="hackrf_sweep_csv",
            size_bytes=csv.stat().st_size,
        )
        session.add(capture)
        session.commit()
        session.refresh(capture)
        capture_id = capture.id
        other = Capture(device="airspy", path="/x/c.npz", format="npz_spectra")
        session.add(other)
        session.commit()
        session.refresh(other)
        other_id = other.id
    client = TestClient(
        create_app(
            Settings(zmq_endpoint="tcp://127.0.0.1:1", data_dir=str(tmp_path)), engine=engine
        )
    )
    return client, capture_id, other_id


def test_the_json_endpoint_carries_occupancy_and_the_verdict(rfi_client):
    client, capture_id, _ = rfi_client
    payload = client.get(f"/api/captures/{capture_id}/rfi").json()
    assert payload["n_sweeps"] == 20
    assert payload["protected_band"]["covered"] is True
    assert payload["protected_band"]["clean"] is False
    assert any(item["character"] == "bursty" for item in payload["interferers"])
    assert any(
        item["allocation"] and "5.340" in item["allocation"] for item in payload["interferers"]
    )


def test_the_plot_renders_both_panels(rfi_client):
    client, capture_id, _ = rfi_client
    response = client.get(f"/api/captures/{capture_id}/rfi.png")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert len(response.content) > 10_000


def test_the_page_leads_with_the_protected_band_verdict(rfi_client):
    client, capture_id, _ = rfi_client
    body = client.get(f"/captures/{capture_id}/rfi").text
    assert "1400" in body and "1427" in body
    assert "5.340" in body
    assert "bursty" in body
    # Regional labels must be marked as unverified so nobody chases the wrong transmitter.
    assert "verify locally" in body
    # And the verdict must appear before the plot, not after it.
    assert body.index("1400–1427 MHz") < body.index("rfi.png")


def test_rfi_endpoints_refuse_a_capture_that_is_not_a_sweep(rfi_client):
    client, _, other_id = rfi_client
    for url in (f"/api/captures/{other_id}/rfi", f"/captures/{other_id}/rfi"):
        response = client.get(url)
        assert response.status_code == 422
        assert "hackrf_sweep" in response.json()["detail"]


def test_rfi_endpoints_404_on_an_unknown_capture(rfi_client):
    client, _, _ = rfi_client
    assert client.get("/api/captures/9999/rfi").status_code == 404
