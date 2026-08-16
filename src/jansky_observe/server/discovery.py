"""mDNS advertisement of the station API (plans/remote-scanner.md).

The server registers ``_jansky-observe._tcp.local.`` so LAN clients — the
remote RFI scanner first — find the station without configuration. This is
the scoped-down first slice of the parked "v2 multi-station mDNS" roadmap
item: *advertisement only*, no browsing, no peering.

Failure is never load-bearing: no ``zeroconf`` package, no usable network,
or ``JANSKY_OBSERVE_NO_MDNS=1`` all degrade to "not advertised" and the
server runs exactly as before. The Pi's own ``raspberrypi.local`` (avahi)
remains the client's documented fallback.
"""

from __future__ import annotations

import logging
import os
import socket
from typing import Any

from jansky_observe import __version__

__all__ = ["SERVICE_TYPE", "advertise", "unregister"]

log = logging.getLogger(__name__)

SERVICE_TYPE = "_jansky-observe._tcp.local."


def _local_ip() -> str:
    """The outbound-interface IP (no packets sent), else loopback."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("10.255.255.255", 1))
            ip: str = probe.getsockname()[0]
            return ip
    except OSError:
        return "127.0.0.1"


def advertise(port: int) -> Any | None:
    """Register the service; returns an opaque handle for :func:`unregister`.

    Returns ``None`` — with a log line, never an exception — when disabled
    via ``JANSKY_OBSERVE_NO_MDNS``, when ``zeroconf`` is not installed, or
    when registration fails.
    """
    if os.environ.get("JANSKY_OBSERVE_NO_MDNS"):
        log.info("mDNS advertisement disabled by JANSKY_OBSERVE_NO_MDNS")
        return None
    try:
        from zeroconf import ServiceInfo, Zeroconf
    except ImportError:
        log.info("zeroconf not installed; station not advertised over mDNS")
        return None
    try:
        hostname = socket.gethostname().split(".")[0]
        info = ServiceInfo(
            SERVICE_TYPE,
            f"{hostname}.{SERVICE_TYPE}",
            addresses=[socket.inet_aton(_local_ip())],
            port=port,
            properties={"path": "/api", "version": __version__},
        )
        zc = Zeroconf()
        zc.register_service(info)
    except Exception as exc:  # noqa: BLE001 — advertisement is best-effort by design
        log.warning("mDNS advertisement failed: %r", exc)
        return None
    log.info("advertising %s on port %d", SERVICE_TYPE, port)
    return (zc, info)


def unregister(handle: Any | None) -> None:
    """Tear down a successful :func:`advertise`; a ``None`` handle is a no-op."""
    if handle is None:
        return
    zc, info = handle
    try:
        zc.unregister_service(info)
        zc.close()
    except Exception as exc:  # noqa: BLE001 — shutdown must never raise
        log.warning("mDNS unregister failed: %r", exc)
