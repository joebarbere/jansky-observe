"""Runtime configuration from environment variables (all optional).

Everything has a LAN-friendly default; the systemd units in ``deploy/`` set
these explicitly so an installed station is self-describing.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

DEFAULT_ZMQ_ENDPOINT = "tcp://127.0.0.1:8410"
DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8000

__all__ = ["DEFAULT_HOST", "DEFAULT_PORT", "DEFAULT_ZMQ_ENDPOINT", "Settings", "settings_from_env"]


@dataclass(frozen=True)
class Settings:
    """Process-level settings shared by the API server and the capture daemon."""

    zmq_endpoint: str = DEFAULT_ZMQ_ENDPOINT
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT


def settings_from_env() -> Settings:
    """Build :class:`Settings` from ``JANSKY_OBSERVE_*`` environment variables."""
    return Settings(
        zmq_endpoint=os.environ.get("JANSKY_OBSERVE_ZMQ_ENDPOINT", DEFAULT_ZMQ_ENDPOINT),
        host=os.environ.get("JANSKY_OBSERVE_HOST", DEFAULT_HOST),
        port=int(os.environ.get("JANSKY_OBSERVE_PORT", str(DEFAULT_PORT))),
    )
