"""Spectral-frame wire formats shared by the capture daemon and the API server.

The capture daemon publishes reduced spectra over ZeroMQ PUB as a two-part
message ``[JSON header, raw float32 payload]``; the API server re-packs the
same frame for WebSocket fan-out to the browser as a single binary blob::

    uint32 LE header length | UTF-8 JSON header | float32 LE power values

Header fields: ``v`` (schema version), ``seq``, ``timestamp`` (unix seconds,
UTC), ``center_freq_hz``, ``sample_rate_hz``, ``n_fft``. Power values are dB
relative to an arbitrary reference, fftshifted so index 0 is the lowest
frequency (``center_freq_hz - sample_rate_hz / 2``).
"""

from __future__ import annotations

import json
import struct
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

SCHEMA_VERSION = 1

__all__ = ["SCHEMA_VERSION", "SpectralFrame", "decode_zmq", "encode_zmq", "pack_ws"]


@dataclass(frozen=True)
class SpectralFrame:
    """One reduced spectrum: the unit that flows daemon → server → browser."""

    seq: int
    timestamp: float
    center_freq_hz: float
    sample_rate_hz: float
    power_db: np.ndarray  # float32, fftshifted: index 0 = lowest frequency

    def header(self) -> dict[str, float | int]:
        """The JSON-serializable frame header."""
        return {
            "v": SCHEMA_VERSION,
            "seq": self.seq,
            "timestamp": self.timestamp,
            "center_freq_hz": self.center_freq_hz,
            "sample_rate_hz": self.sample_rate_hz,
            "n_fft": int(self.power_db.size),
        }

    def frequencies_hz(self) -> np.ndarray:
        """Absolute frequency axis (Hz) matching ``power_db``."""
        n = self.power_db.size
        return self.center_freq_hz + (np.arange(n) - n / 2) * (self.sample_rate_hz / n)


def encode_zmq(frame: SpectralFrame) -> list[bytes]:
    """Encode a frame as a ZeroMQ multipart message ``[header, payload]``."""
    payload = np.ascontiguousarray(frame.power_db, dtype="<f4").tobytes()
    return [json.dumps(frame.header()).encode(), payload]


def decode_zmq(parts: Sequence[bytes]) -> SpectralFrame:
    """Decode a ZeroMQ multipart message produced by :func:`encode_zmq`."""
    if len(parts) != 2:
        raise ValueError(f"expected 2 message parts, got {len(parts)}")
    header = json.loads(parts[0])
    if header.get("v") != SCHEMA_VERSION:
        raise ValueError(f"unsupported frame schema version: {header.get('v')!r}")
    power = np.frombuffer(parts[1], dtype="<f4")
    if power.size != header["n_fft"]:
        raise ValueError(f"payload has {power.size} bins, header says {header['n_fft']}")
    return SpectralFrame(
        seq=int(header["seq"]),
        timestamp=float(header["timestamp"]),
        center_freq_hz=float(header["center_freq_hz"]),
        sample_rate_hz=float(header["sample_rate_hz"]),
        power_db=power,
    )


def pack_ws(frame: SpectralFrame) -> bytes:
    """Pack a frame for the browser WebSocket (see module docstring for layout)."""
    header, payload = encode_zmq(frame)
    return struct.pack("<I", len(header)) + header + payload
