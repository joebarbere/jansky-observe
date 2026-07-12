"""The SDR-owning capture daemon: sources, DSP, device profiles, ZMQ publisher.

Sources: ``jansky-observe-capture --source synthetic`` (noise + fake-HI, no
hardware) or ``--source airspy`` (the real Airspy Mini via an ``airspy_rx``
subprocess), both behind the same
:class:`~jansky_observe.capture.sources.SDRSource` protocol. Captures are
written by :mod:`jansky_observe.capture.writer` (``.npz`` spectra, SigMF IQ)
under control-channel command (:mod:`jansky_observe.control`).

SAFETY: device profiles live in :mod:`jansky_observe.capture.profiles`,
which encodes the bias-tee rule — the Airspy internal bias tee is never
enabled in the H-line profile; :mod:`jansky_observe.capture.airspy_cli` is a
guarded path. Read those modules before touching anything hardware-facing.
"""

from __future__ import annotations

from jansky_observe.capture.airspy_cli import AirspyRxSource, build_airspy_cmd
from jansky_observe.capture.dsp import welch_psd_db
from jansky_observe.capture.profiles import (
    HLINE_AIRSPY,
    BiasTeeForbiddenError,
    DeviceProfile,
    validate_profile,
)
from jansky_observe.capture.sources import SDRSource, SyntheticHISource
from jansky_observe.capture.writer import NpzCaptureWriter, SigmfCaptureWriter

__all__ = [
    "HLINE_AIRSPY",
    "AirspyRxSource",
    "BiasTeeForbiddenError",
    "DeviceProfile",
    "NpzCaptureWriter",
    "SDRSource",
    "SigmfCaptureWriter",
    "SyntheticHISource",
    "build_airspy_cmd",
    "validate_profile",
    "welch_psd_db",
]
