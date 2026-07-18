"""Reference HI profile client (roadmap M12) — the seam to jansky-research plan 78.

Returns the *expected* 21 cm profile (v_LSR vs brightness temperature) for a
galactic direction, so the spectrum view can overlay a model on the observed data
(the visual half of the deferred ``hi4pi_xcheck``). It is a thin, **pluggable**
client, never a survey store:

* ``provider="web"`` — best-effort fetch from the LAB survey's EU-HOU profile tool
  (the same source Virgo's ``simulate()`` uses), cached to disk. Degrades to
  ``None`` offline / on any error; the overlay is optional context.
* ``provider="file"`` — read a profile dropped into the cache dir (e.g. one that
  **jansky-research plan 78's tool** produced). This is the offline authoritative
  path; the survey handling lives there, not here.

The overlay/model is an advisory visual aid, never a detection verdict (the
quantitative cross-check stays in jansky-research plan 78, plan §6).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import astropy.units as u
import numpy as np
from astropy.coordinates import SkyCoord

__all__ = ["LAB_PROFILE_URL", "ReferenceProfile", "reference_profile"]

logger = logging.getLogger(__name__)

#: The LAB survey EU-HOU profile endpoint (same source Virgo's simulate() uses).
LAB_PROFILE_URL = "https://www.astro.uni-bonn.de/hisurvey/euhou/LABprofile/download.php"
_HTTP_TIMEOUT_S = 12.0
_GRID_DEG = 0.5  # LAB is ~0.5° sampled; round the query + cache key to it.


@dataclass(frozen=True)
class ReferenceProfile:
    """An expected 21 cm profile for a galactic direction.

    ``v_lsr_kms`` and ``t_b_k`` are equal-length arrays (velocity axis + brightness
    temperature in kelvin); ``source`` names the survey ("LAB"); ``l_deg``/``b_deg``
    is the (rounded) galactic direction it was retrieved for.
    """

    v_lsr_kms: np.ndarray
    t_b_k: np.ndarray
    source: str
    l_deg: float
    b_deg: float

    @property
    def peak_t_b_k(self) -> float:
        """The model's peak brightness temperature (K), or 0.0 if empty."""
        return float(self.t_b_k.max()) if self.t_b_k.size else 0.0


def _round_grid(value: float) -> float:
    return round(value / _GRID_DEG) * _GRID_DEG


def _cache_path(cache_dir: Path, l_deg: float, b_deg: float) -> Path:
    return cache_dir / f"lab_{l_deg:+.1f}_{b_deg:+.1f}.npz"


def _lab_profile_text(l_deg: float, b_deg: float, *, timeout_s: float = _HTTP_TIMEOUT_S) -> str:
    """Fetch the raw LAB profile text for a galactic direction (may raise).

    Converts (l, b) to equatorial and POSTs to :data:`LAB_PROFILE_URL`, which
    returns a two-column ``velocity  brightness_temperature`` table. Isolated so
    tests monkeypatch it — no network in the test suite.
    """
    import httpx

    icrs = SkyCoord(l=l_deg * u.deg, b=b_deg * u.deg, frame="galactic").icrs
    params = {
        "ra": f"{icrs.ra.deg:.5f}",
        "dec": f"{icrs.dec.deg:.5f}",
        "csys": "0",  # equatorial
        "beam": "1",
    }
    response = httpx.get(LAB_PROFILE_URL, params=params, timeout=timeout_s)
    response.raise_for_status()
    return response.text


def _parse_lab_profile(text: str) -> tuple[np.ndarray, np.ndarray]:
    """Parse two numeric columns (velocity, brightness temperature) from LAB text."""
    velocities: list[float] = []
    temps: list[float] = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        try:
            v, t = float(parts[0]), float(parts[1])
        except ValueError:
            continue  # header / comment line
        velocities.append(v)
        temps.append(t)
    return (
        np.asarray(velocities, dtype=np.float64),
        np.asarray(temps, dtype=np.float64),
    )


def _load_cached(path: Path, l_deg: float, b_deg: float) -> ReferenceProfile | None:
    if not path.exists():
        return None
    try:
        with np.load(path, allow_pickle=False) as data:
            return ReferenceProfile(
                v_lsr_kms=np.asarray(data["v_lsr_kms"], dtype=np.float64),
                t_b_k=np.asarray(data["t_b_k"], dtype=np.float64),
                source="LAB",
                l_deg=l_deg,
                b_deg=b_deg,
            )
    except Exception:  # noqa: BLE001 — a bad cache file must not break the overlay
        return None


def reference_profile(
    l_deg: float,
    b_deg: float,
    *,
    provider: str = "web",
    cache_dir: str | Path | None = None,
) -> ReferenceProfile | None:
    """The expected LAB HI profile for a galactic direction, or ``None``.

    Parameters
    ----------
    l_deg, b_deg : float
        Galactic longitude/latitude in degrees (rounded to the ~0.5° LAB grid for
        the query and the cache key).
    provider : str
        ``"web"`` (best-effort fetch + cache) or ``"file"`` (cache/dropped file
        only, no network — the jansky-research plan 78 path).
    cache_dir : str or Path, optional
        Directory for cached profiles. Required for ``"file"``; for ``"web"`` it
        enables offline reuse of a prior fetch.

    Returns
    -------
    ReferenceProfile or None
        The profile, or ``None`` when unavailable (offline, no file, parse
        failure, empty result) — never raises. The overlay treats ``None`` as
        "model unavailable".
    """
    lr, br = _round_grid(l_deg), _round_grid(b_deg)
    cache = Path(cache_dir) if cache_dir is not None else None

    if cache is not None:
        cached = _load_cached(_cache_path(cache, lr, br), lr, br)
        if cached is not None:
            return cached

    if provider != "web":
        return None  # "file" provider: only the cache/dropped file, checked above

    try:
        v, t = _parse_lab_profile(_lab_profile_text(lr, br))
    except Exception as exc:  # noqa: BLE001 — best-effort; the overlay is optional
        logger.info("LAB profile fetch for l=%.1f b=%.1f failed: %s", lr, br, exc)
        return None
    if v.size == 0 or t.size == 0:
        return None

    if cache is not None:
        cache.mkdir(parents=True, exist_ok=True)
        np.savez(_cache_path(cache, lr, br), v_lsr_kms=v, t_b_k=t)
    return ReferenceProfile(v_lsr_kms=v, t_b_k=t, source="LAB", l_deg=lr, b_deg=br)
