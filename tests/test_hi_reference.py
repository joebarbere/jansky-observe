"""Reference HI profile client (roadmap M12) — no real network (fetch mocked)."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from jansky_observe.astro import hi_reference
from jansky_observe.astro.hi_reference import ReferenceProfile, reference_profile

_SAMPLE_TEXT = """# LAB profile
# velocity  T_b
-100.0   0.5
 -50.0   3.0
   0.0  40.0
  50.0   2.0
 100.0   0.4
"""


def test_parse_lab_profile_extracts_two_columns() -> None:
    v, t = hi_reference._parse_lab_profile(_SAMPLE_TEXT)
    assert v.tolist() == [-100.0, -50.0, 0.0, 50.0, 100.0]
    assert t[2] == 40.0  # the line peak


def test_web_provider_returns_and_caches(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[float, float]] = []

    def fake_fetch(l_deg: float, b_deg: float, *, timeout_s: float = 0.0) -> str:
        calls.append((l_deg, b_deg))
        return _SAMPLE_TEXT

    monkeypatch.setattr(hi_reference, "_lab_profile_text", fake_fetch)
    prof = reference_profile(120.3, 0.1, provider="web", cache_dir=tmp_path)
    assert isinstance(prof, ReferenceProfile)
    assert prof.source == "LAB"
    assert prof.l_deg == 120.5 and prof.b_deg == 0.0  # rounded to the 0.5° grid
    assert prof.peak_t_b_k == 40.0
    assert len(calls) == 1

    # Second call is served from the cache — the fetch is not hit again even if it
    # would now fail.
    monkeypatch.setattr(
        hi_reference, "_lab_profile_text", lambda *a, **k: (_ for _ in ()).throw(RuntimeError())
    )
    again = reference_profile(120.3, 0.1, provider="web", cache_dir=tmp_path)
    assert again is not None and again.peak_t_b_k == 40.0


def test_web_provider_degrades_to_none_on_error(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        hi_reference,
        "_lab_profile_text",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("net")),
    )
    assert reference_profile(30.0, 0.0, provider="web", cache_dir=tmp_path) is None


def test_web_provider_empty_result_is_none(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hi_reference, "_lab_profile_text", lambda *a, **k: "# only headers\n")
    assert reference_profile(30.0, 0.0, provider="web", cache_dir=tmp_path) is None


def test_file_provider_reads_only_the_cache(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    # No file yet → None, and the file provider never touches the network.
    monkeypatch.setattr(
        hi_reference, "_lab_profile_text", lambda *a, **k: (_ for _ in ()).throw(AssertionError())
    )
    assert reference_profile(30.0, 0.0, provider="file", cache_dir=tmp_path) is None

    # Drop a profile (as jansky-research plan 78's tool would) → the file provider reads it.
    path = hi_reference._cache_path(tmp_path, 30.0, 0.0)
    np.savez(path, v_lsr_kms=np.array([-20.0, 0.0, 20.0]), t_b_k=np.array([1.0, 50.0, 1.0]))
    prof = reference_profile(30.0, 0.0, provider="file", cache_dir=tmp_path)
    assert prof is not None and prof.peak_t_b_k == 50.0


def test_no_cache_dir_no_network_is_none() -> None:
    # file provider with no cache dir → nothing to read → None (no crash).
    assert reference_profile(30.0, 0.0, provider="file") is None


# ---- per-pointing memo (plans/calibrated-overlay.md) ------------------------------


def test_memo_serves_a_repeat_pointing_without_touching_disk(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An observation whose captures share a pointing does the (l, b) work once."""
    hi_reference.clear_profile_memo()
    monkeypatch.setattr(hi_reference, "_lab_profile_text", lambda *a, **k: _SAMPLE_TEXT)
    first = reference_profile(30.0, 0.0, provider="web", cache_dir=str(tmp_path))
    assert first is not None

    loads: list[Path] = []
    real_load = hi_reference._load_cached
    monkeypatch.setattr(
        hi_reference,
        "_load_cached",
        lambda path, l_deg, b_deg: (loads.append(path), real_load(path, l_deg, b_deg))[1],
    )
    second = reference_profile(30.0, 0.0, provider="web", cache_dir=str(tmp_path))

    assert second is first  # same object, not merely equal
    assert loads == []  # the disk was never consulted


def test_memo_rounds_to_the_lab_grid() -> None:
    """Pointings inside one 0.5° LAB cell share a memo entry."""
    hi_reference.clear_profile_memo()
    profile = ReferenceProfile(
        v_lsr_kms=np.array([0.0]), t_b_k=np.array([1.0]), source="LAB", l_deg=30.0, b_deg=0.0
    )
    hi_reference._remember((30.0, 0.0, "web", ""), profile)
    assert reference_profile(30.1, 0.04, provider="web") is profile


def test_memo_does_not_let_web_shadow_the_file_provider(tmp_path) -> None:
    """provider is part of the key on purpose.

    ``file`` is plan 78's authoritative path; a profile the web provider happened to
    memoize first must not be served in its place."""
    hi_reference.clear_profile_memo()
    web_profile = ReferenceProfile(
        v_lsr_kms=np.array([0.0]), t_b_k=np.array([1.0]), source="LAB", l_deg=30.0, b_deg=0.0
    )
    hi_reference._remember((30.0, 0.0, "web", str(tmp_path)), web_profile)
    assert reference_profile(30.0, 0.0, provider="file", cache_dir=str(tmp_path)) is None


def test_memo_does_not_cache_failures(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A transient outage must not become permanent for the process's lifetime."""
    hi_reference.clear_profile_memo()
    monkeypatch.setattr(
        hi_reference,
        "_lab_profile_text",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("offline")),
    )
    assert reference_profile(30.0, 0.0, provider="web", cache_dir=str(tmp_path)) is None
    assert hi_reference.profile_memo_size() == 0

    monkeypatch.setattr(hi_reference, "_lab_profile_text", lambda *a, **k: _SAMPLE_TEXT)
    assert reference_profile(30.0, 0.0, provider="web", cache_dir=str(tmp_path)) is not None


def test_memoized_arrays_are_read_only(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A caller mutating an array in place would poison every later render."""
    hi_reference.clear_profile_memo()
    monkeypatch.setattr(hi_reference, "_lab_profile_text", lambda *a, **k: _SAMPLE_TEXT)
    profile = reference_profile(30.0, 0.0, provider="web", cache_dir=str(tmp_path))
    assert profile is not None
    with pytest.raises(ValueError):
        profile.t_b_k[0] = 999.0


def test_memo_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    hi_reference.clear_profile_memo()
    monkeypatch.setattr(hi_reference, "_MEMO_MAX", 4)
    for i in range(10):
        hi_reference._remember(
            (float(i), 0.0, "web", ""),
            ReferenceProfile(
                v_lsr_kms=np.array([0.0]),
                t_b_k=np.array([1.0]),
                source="LAB",
                l_deg=float(i),
                b_deg=0.0,
            ),
        )
    assert hi_reference.profile_memo_size() == 4
    # An evicted pointing must fall through to a real lookup, so block the network.
    monkeypatch.setattr(
        hi_reference,
        "_lab_profile_text",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not fetch")),
    )
    # Least-recently-used entries were evicted, newest kept.
    assert reference_profile(9.0, 0.0, provider="web") is not None
    assert reference_profile(0.0, 0.0, provider="web") is None


def test_different_cache_dirs_do_not_share_a_memo_entry(tmp_path) -> None:
    """The cache dir is part of the key — a test tmpdir must not leak into another."""
    hi_reference.clear_profile_memo()
    profile = ReferenceProfile(
        v_lsr_kms=np.array([0.0]), t_b_k=np.array([1.0]), source="LAB", l_deg=30.0, b_deg=0.0
    )
    hi_reference._remember((30.0, 0.0, "file", str(tmp_path / "a")), profile)
    assert reference_profile(30.0, 0.0, provider="file", cache_dir=str(tmp_path / "b")) is None
