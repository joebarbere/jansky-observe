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
from typing import IO, Any

import numpy as np

from jansky_observe import __version__
from jansky_observe.frames import SpectralFrame

__all__ = ["NpzCaptureWriter", "SigmfCaptureWriter"]


class NpzCaptureWriter:
    """Stream spectral frames to disk; assemble one ``.npz`` on :meth:`close`.

    Each ``power_db`` row is appended to a temporary raw ``float32`` file as it
    arrives, rather than retaining every :class:`SpectralFrame` in a list until
    close. Memory stays O(1) in capture length — a multi-hour unattended run (the
    scheduler / drift-scan campaigns) no longer accumulates ~100 MB+/hour of live
    Python objects, and close no longer spikes RAM with an ``np.stack`` of the whole
    history. The output ``.npz`` is byte-identical to the old in-memory path (the
    same ``power_db`` 2-D ``float32`` array, ``timestamps``, scalars and settings).
    """

    def __init__(self, path: str | Path, settings: dict[str, Any] | None = None) -> None:
        self.path = Path(path)
        self._settings = dict(settings or {})
        # Rows spill here as raw little-endian float32, C-order (n_frames, n_fft).
        self._rows_path = self.path.parent / (self.path.name + ".rows.tmp")
        self._rows: IO[bytes] | None = None  # opened lazily on the first frame
        self._timestamps: list[float] = []  # 8 B/frame — cheap to hold
        self._n_fft: int | None = None
        self._n_frames = 0
        self._first_axes: tuple[float, float] | None = None  # (center_hz, rate_hz)
        self.bytes_written = 0

    def add_frame(self, frame: SpectralFrame) -> None:
        """Record one published frame (streamed straight to the spill file)."""
        row = np.ascontiguousarray(frame.power_db, dtype="<f4")
        if self._rows is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._rows = open(self._rows_path, "wb")  # noqa: SIM115  # closed in close()
            self._n_fft = row.size
            self._first_axes = (frame.center_freq_hz, frame.sample_rate_hz)
        elif row.size != self._n_fft:
            raise ValueError(
                f"frame n_fft changed within a capture: {row.size} != {self._n_fft}"
            )
        self._rows.write(row.tobytes())
        self._timestamps.append(float(frame.timestamp))
        self._n_frames += 1
        self.bytes_written += row.nbytes + 8

    def close(self) -> Path:
        """Assemble the ``.npz`` from the spill file and return its path."""
        if self._rows is None:
            raise RuntimeError("no frames captured")
        self._rows.close()
        assert self._n_fft is not None and self._first_axes is not None
        center_freq_hz, sample_rate_hz = self._first_axes
        # memmap the spill file so np.savez streams it into the zip in bounded-size
        # chunks (never materializing the full history in RAM).
        power_db = np.memmap(
            self._rows_path, dtype="<f4", mode="r", shape=(self._n_frames, self._n_fft)
        )
        try:
            np.savez(
                self.path,
                power_db=power_db,
                timestamps=np.array(self._timestamps, dtype=np.float64),
                center_freq_hz=center_freq_hz,
                sample_rate_hz=sample_rate_hz,
                settings=json.dumps({**self._settings, "software_version": __version__}),
            )
        finally:
            del power_db  # release the mapping before removing the spill file
            self._rows_path.unlink(missing_ok=True)
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
