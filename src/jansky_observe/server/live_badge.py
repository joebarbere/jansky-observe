"""The live "am I seeing it?" badge (plan §5.2, §6): server-side accumulating
average + running classification.

The ZMQ relay feeds every decoded :class:`~jansky_observe.frames.SpectralFrame`
to a process-wide :class:`LiveBadge`, which accumulates LINEAR power (averaging
dB would bias low — same rule as :mod:`jansky_observe.confirm.baseline`) and
auto-resets whenever the stream parameters change. :meth:`LiveBadge.snapshot`
classifies the running average with the deterministic ``hline_v1`` classifier:
the Doppler window comes from astropy at the current pointing/time when an
observation is running (plan §6), else the fixed fallback window.

``add_frame`` runs on the event loop; ``snapshot`` runs in a worker thread
(the astropy LSR window costs ~100 ms), so state is guarded by a lock.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import datetime
from typing import Any

import numpy as np
from astropy.coordinates import SkyCoord
from sqlmodel import Session, col, select

from jansky_observe.astro.lsr import HI_LINE_FREQ_HZ, doppler_window_hz, vlsr_axis
from jansky_observe.astro.pointing import target_coord
from jansky_observe.confirm.baseline import db_to_linear, linear_to_db
from jansky_observe.confirm.classifier import FIXED_WINDOW_HALF_WIDTH_HZ, running_classify
from jansky_observe.frames import SpectralFrame
from jansky_observe.models import Location, Observation, RadioSource, utcnow

__all__ = ["MIN_FRAMES", "LiveBadge", "SessionFactory", "source_coord"]

MIN_FRAMES = 10
"""Frames accumulated before the badge starts classifying."""

SessionFactory = Callable[[], Session]
"""Opens a database session; the badge queries the running observation with it."""

_FIXED_WINDOW = (
    HI_LINE_FREQ_HZ - FIXED_WINDOW_HALF_WIDTH_HZ,
    HI_LINE_FREQ_HZ + FIXED_WINDOW_HALF_WIDTH_HZ,
)


def source_coord(source: RadioSource, when: datetime | None = None) -> SkyCoord:
    """Build the pointing :class:`~astropy.coordinates.SkyCoord` for a source row.

    Parameters
    ----------
    source : RadioSource
        The catalog row: the Sun computes an ephemeris, galactic l/b wins over
        RA/Dec when both are present (HI targets along the plane).
    when : datetime, optional
        UTC time for the Sun ephemeris; ignored for fixed targets.

    Returns
    -------
    SkyCoord
        The target coordinate.
    """
    if source.kind == "sun":
        return target_coord("sun", when=when)
    if source.gal_l_deg is not None and source.gal_b_deg is not None:
        return target_coord("galactic", gal_l_deg=source.gal_l_deg, gal_b_deg=source.gal_b_deg)
    return target_coord("radec", ra_deg=source.ra_deg, dec_deg=source.dec_deg)


class LiveBadge:
    """Accumulating linear-power average with a running HI verdict.

    Parameters
    ----------
    min_frames : int
        Frames required before :meth:`snapshot` classifies (default
        :data:`MIN_FRAMES`); below that it reports ``"accumulating"``.
    """

    def __init__(self, min_frames: int = MIN_FRAMES) -> None:
        self._min_frames = min_frames
        self._lock = threading.Lock()
        self._key: tuple[float, float, int] | None = None
        self._sum: np.ndarray | None = None
        self._count = 0
        self._first_timestamp = 0.0
        self._last_timestamp = 0.0

    @property
    def n_frames(self) -> int:
        """Frames accumulated since the last (auto-)reset."""
        with self._lock:
            return self._count

    def add_frame(self, frame: SpectralFrame) -> None:
        """Accumulate one frame in LINEAR power.

        Any change in stream parameters (center frequency, sample rate, FFT
        size) invalidates the running mean and restarts accumulation — same
        rule as the browser's accumulating average.
        """
        key = (frame.center_freq_hz, frame.sample_rate_hz, int(frame.power_db.size))
        linear = db_to_linear(frame.power_db)
        with self._lock:
            if key != self._key or self._sum is None:
                self._key = key
                self._sum = np.zeros(key[2], dtype=np.float64)
                self._count = 0
                self._first_timestamp = frame.timestamp
            self._sum += linear
            self._count += 1
            self._last_timestamp = frame.timestamp

    def reset(self) -> None:
        """Clear the accumulator (the badge's reset link / MCP safe verb)."""
        with self._lock:
            self._key = None
            self._sum = None
            self._count = 0
            self._first_timestamp = 0.0
            self._last_timestamp = 0.0

    def snapshot(self, session_factory: SessionFactory) -> dict[str, Any]:
        """Classify the current average; JSON-able badge payload.

        With fewer than ``min_frames`` frames the payload is
        ``{"status": "accumulating", "n_frames": n}``. Otherwise the average
        is classified inside the Doppler window: from
        :func:`~jansky_observe.astro.lsr.doppler_window_hz` at the running
        observation's pointing/location and *now* when one exists
        (``window_source="lsr"``), else the fixed fallback window
        (``window_source="fixed"``).

        Parameters
        ----------
        session_factory : SessionFactory
            Opens a session for the running-observation lookup (only called
            once enough frames have accumulated).

        Returns
        -------
        dict
            ``{"status": "ok", "verdict", "snr", "peak_freq_hz", "n_frames",
            "elapsed_s", "window_source", "window_hz", "name", "version"}``
            plus ``"peak_vlsr_kms"`` when the window is LSR-derived; or the
            accumulating payload; or ``{"status": "error", "detail", ...}``
            when the window misses the streamed band entirely.
        """
        with self._lock:
            if self._key is None or self._sum is None or self._count < self._min_frames:
                return {"status": "accumulating", "n_frames": self._count}
            center_freq_hz, sample_rate_hz, n_fft = self._key
            avg_linear = self._sum / self._count
            n_frames = self._count
            elapsed_s = self._last_timestamp - self._first_timestamp

        freq_hz = center_freq_hz + (np.arange(n_fft) - n_fft / 2) * (sample_rate_hz / n_fft)
        avg_db = linear_to_db(avg_linear)
        window_hz, window_source, pointing = self._window(session_factory)
        try:
            verdict = running_classify(freq_hz, avg_db, window_hz)
        except ValueError as exc:
            return {"status": "error", "detail": str(exc), "n_frames": n_frames}

        payload: dict[str, Any] = {
            "status": "ok",
            "verdict": verdict.verdict,
            "snr": verdict.score,
            "peak_freq_hz": verdict.params["peak_freq_hz"],
            "n_frames": n_frames,
            "elapsed_s": elapsed_s,
            "window_source": window_source,
            "window_hz": [window_hz[0], window_hz[1]],
            "name": verdict.name,
            "version": verdict.version,
        }
        if pointing is not None:
            coord, location, when = pointing
            velocity = vlsr_axis(
                np.array([payload["peak_freq_hz"]]),
                coord,
                location.lat_deg,
                location.lon_deg,
                location.elevation_m,
                when,
            )
            payload["peak_vlsr_kms"] = float(velocity[0])
        return payload

    @staticmethod
    def _window(
        session_factory: SessionFactory,
    ) -> tuple[tuple[float, float], str, tuple[SkyCoord, Location, datetime] | None]:
        """The Doppler window plus the pointing context it came from.

        Returns ``(window_hz, window_source, pointing)`` where ``pointing``
        is ``(coord, location, when)`` for the LSR case and ``None`` for the
        fixed fallback.
        """
        now = utcnow()
        with session_factory() as session:
            observation = session.exec(
                select(Observation)
                .where(Observation.status == "running")
                .order_by(col(Observation.id).desc())
            ).first()
            if observation is not None:
                source = session.get(RadioSource, observation.source_id)
                location = session.get(Location, observation.location_id)
                if source is not None and location is not None:
                    coord = source_coord(source, when=now)
                    window = doppler_window_hz(
                        coord, location.lat_deg, location.lon_deg, location.elevation_m, now
                    )
                    return window, "lsr", (coord, location, now)
        return _FIXED_WINDOW, "fixed", None
