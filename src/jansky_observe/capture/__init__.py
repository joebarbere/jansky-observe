"""The SDR-owning capture daemon: sources, DSP, device profiles, ZMQ publisher.

At M0 everything is synthetic (no hardware): ``jansky-observe-capture
--synthetic`` streams noise + fake-HI spectral frames over ZeroMQ PUB. The
real Airspy Mini source lands at M1 behind the same
:class:`~jansky_observe.capture.sources.SDRSource` protocol.

SAFETY: device profiles live in :mod:`jansky_observe.capture.profiles`,
which encodes the bias-tee rule — the Airspy internal bias tee is never
enabled in the H-line profile. Read that module before touching anything
hardware-facing.
"""

from __future__ import annotations

from jansky_observe.capture.dsp import welch_psd_db
from jansky_observe.capture.profiles import (
    HLINE_AIRSPY,
    BiasTeeForbiddenError,
    DeviceProfile,
    validate_profile,
)
from jansky_observe.capture.sources import SDRSource, SyntheticHISource

__all__ = [
    "HLINE_AIRSPY",
    "BiasTeeForbiddenError",
    "DeviceProfile",
    "SDRSource",
    "SyntheticHISource",
    "validate_profile",
    "welch_psd_db",
]
