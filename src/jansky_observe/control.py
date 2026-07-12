"""Control-channel protocol between the API server and the capture daemon.

The daemon binds a ZeroMQ REP socket next to its PUB frame stream; the API
server sends single-shot JSON requests over a REQ socket. Requests::

    {"cmd": "status"}
    {"cmd": "start_capture", "format": "npz" | "sigmf"}
    {"cmd": "stop_capture"}

Every reply carries ``{"ok": bool}`` plus, on failure, ``{"error": str}``.
A ``status`` reply (and the replies to start/stop, which embed the new
status) includes::

    {
      "capturing": bool,
      "format": "npz" | "sigmf" | null,
      "path": str | null,            # capture file (base path for SigMF)
      "bytes_written": int,
      "elapsed_s": float,
      "rate_bytes_per_s": float,     # projected write rate for the format
      "disk_free_bytes": int,        # free space on the data volume
      "source": str,                 # e.g. "synthetic", "airspy"
    }

:func:`ctl_request` is the client side: REQ with a poll timeout, socket
discarded on timeout (a REQ socket that missed its reply is unusable — the
"lazy pirate" rule), so a dead daemon degrades to an error, never a hang.
"""

from __future__ import annotations

import json
from typing import Any

import zmq

CTL_TIMEOUT_MS = 2000

__all__ = ["CTL_TIMEOUT_MS", "CaptureFormat", "ctl_request"]

CaptureFormat = str  # "npz" | "sigmf" — validated at the daemon


def ctl_request(
    endpoint: str,
    request: dict[str, Any],
    *,
    timeout_ms: int = CTL_TIMEOUT_MS,
    context: zmq.Context | None = None,
) -> dict[str, Any]:
    """Send one control request; return the reply dict.

    Returns ``{"ok": False, "error": "..."}`` instead of raising when the
    daemon is unreachable or the reply times out.
    """
    ctx = context if context is not None else zmq.Context.instance()
    socket = ctx.socket(zmq.REQ)
    socket.setsockopt(zmq.LINGER, 0)
    try:
        socket.connect(endpoint)
        socket.send(json.dumps(request).encode())
        if socket.poll(timeout_ms, zmq.POLLIN) == 0:
            return {"ok": False, "error": f"capture daemon did not reply within {timeout_ms} ms"}
        reply = json.loads(socket.recv())
        if not isinstance(reply, dict):
            return {"ok": False, "error": "malformed reply from capture daemon"}
        return reply
    except zmq.ZMQError as exc:
        return {"ok": False, "error": f"control channel error: {exc}"}
    finally:
        socket.close(linger=0)
