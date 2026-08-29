"""Runtime configuration from environment variables (all optional).

Everything has a LAN-friendly default; the systemd units in ``deploy/`` set
these explicitly so an installed station is self-describing.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

DEFAULT_ZMQ_ENDPOINT = "tcp://127.0.0.1:8410"
DEFAULT_CTL_ENDPOINT = "tcp://127.0.0.1:8411"
DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8000
DEFAULT_DATA_DIR = "data"
#: Main-beam efficiency. 1.0 means NONE APPLIED, so the calibrated overlay's axis is
#: antenna temperature T_A, not brightness temperature T_B. A small dish with a simple
#: feed is typically 0.5-0.7; measuring it needs a calibrator of known flux, so it stays
#: an explicit station constant rather than a guess (plans/calibrated-overlay.md).
DEFAULT_ETA_MB = 1.0

__all__ = [
    "DEFAULT_CTL_ENDPOINT",
    "DEFAULT_DATA_DIR",
    "DEFAULT_ETA_MB",
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "DEFAULT_ZMQ_ENDPOINT",
    "Settings",
    "settings_from_env",
]


@dataclass(frozen=True)
class Settings:
    """Process-level settings shared by the API server and the capture daemon."""

    zmq_endpoint: str = DEFAULT_ZMQ_ENDPOINT
    ctl_endpoint: str = DEFAULT_CTL_ENDPOINT
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    data_dir: str = DEFAULT_DATA_DIR
    eta_mb: float = DEFAULT_ETA_MB


def settings_from_env() -> Settings:
    """Build :class:`Settings` from ``JANSKY_OBSERVE_*`` environment variables."""
    return Settings(
        zmq_endpoint=os.environ.get("JANSKY_OBSERVE_ZMQ_ENDPOINT", DEFAULT_ZMQ_ENDPOINT),
        ctl_endpoint=os.environ.get("JANSKY_OBSERVE_CTL_ENDPOINT", DEFAULT_CTL_ENDPOINT),
        host=os.environ.get("JANSKY_OBSERVE_HOST", DEFAULT_HOST),
        port=int(os.environ.get("JANSKY_OBSERVE_PORT", str(DEFAULT_PORT))),
        data_dir=os.environ.get("JANSKY_OBSERVE_DATA_DIR", DEFAULT_DATA_DIR),
        eta_mb=float(os.environ.get("JANSKY_OBSERVE_ETA_MB", str(DEFAULT_ETA_MB))),
    )
