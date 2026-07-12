"""Capture writers: ``.npz`` spectra and streaming SigMF IQ.

Both record the full SDR settings and software version — the plan's
reproducibility rule: every capture is self-describing.

- :class:`NpzCaptureWriter` accumulates published spectral frames and writes
  one ``.npz`` at close (compact; ~KB/s).
- :class:`SigmfCaptureWriter` streams interleaved INT16 I/Q (``ci16_le`` —
  bit-faithful to the Airspy's ADC, half the disk of float32: ~43 GB/h at
  3 MSPS) to ``<base>.sigmf-data`` incrementally, writing the
  ``<base>.sigmf-meta`` JSON at close, readable by
  :func:`jansky.formats.read_sigmf`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np

from jansky_observe import __version__
from jansky_observe.frames import SpectralFrame

__all__ = ["NpzCaptureWriter", "SigmfCaptureWriter"]


class NpzCaptureWriter:
    """Accumulate spectral frames; write one ``.npz`` on :meth:`close`."""

    def __init__(self, path: str | Path, settings: dict[str, Any] | None = None) -> None:
        self.path = Path(path)
        self._settings = dict(settings or {})
        self._frames: list[SpectralFrame] = []
        self.bytes_written = 0

    def add_frame(self, frame: SpectralFrame) -> None:
        """Record one published frame."""
        self._frames.append(frame)
        self.bytes_written += frame.power_db.nbytes + 8

    def close(self) -> Path:
        """Write the ``.npz`` and return its path."""
        if not self._frames:
            raise RuntimeError("no frames captured")
        first = self._frames[0]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        np.savez(
            self.path,
            power_db=np.stack([f.power_db for f in self._frames]),
            timestamps=np.array([f.timestamp for f in self._frames], dtype=np.float64),
            center_freq_hz=first.center_freq_hz,
            sample_rate_hz=first.sample_rate_hz,
            settings=json.dumps({**self._settings, "software_version": __version__}),
        )
        self.bytes_written = self.path.stat().st_size
        return self.path


class SigmfCaptureWriter:
    """Stream interleaved INT16 I/Q to disk as a SigMF recording."""

    def __init__(
        self,
        basepath: str | Path,
        *,
        sample_rate_hz: float,
        center_freq_hz: float,
        settings: dict[str, Any] | None = None,
    ) -> None:
        self.basepath = Path(basepath)
        self._sample_rate_hz = sample_rate_hz
        self._center_freq_hz = center_freq_hz
        self._settings = dict(settings or {})
        self.basepath.parent.mkdir(parents=True, exist_ok=True)
        self._data = open(self.basepath.with_suffix(".sigmf-data"), "wb")  # noqa: SIM115
        self.bytes_written = 0

    def write(self, values: np.ndarray) -> None:
        """Append interleaved int16 I/Q values to the data file."""
        raw = np.ascontiguousarray(values, dtype="<i2")
        self._data.write(raw.tobytes())
        self.bytes_written += raw.nbytes

    def close(self) -> Path:
        """Flush data and write the ``.sigmf-meta``; return the meta path."""
        self._data.close()
        meta = {
            "global": {
                "core:datatype": "ci16_le",
                "core:sample_rate": self._sample_rate_hz,
                "core:version": "1.0.0",
                "core:description": "jansky-observe capture",
                "jansky_observe:settings": self._settings,
                "jansky_observe:version": __version__,
            },
            "captures": [{"core:sample_start": 0, "core:frequency": self._center_freq_hz}],
            "annotations": [],
        }
        meta_path = self.basepath.with_suffix(".sigmf-meta")
        meta_path.write_text(json.dumps(meta, indent=2))
        return meta_path
