"""Reference HI profile client (roadmap M12) — no real network (fetch mocked)."""

from __future__ import annotations

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
