"""The station's MCP surface — Claude as a console peer of the browser UI.

Mounted at ``/mcp`` on the API server (plan §12.4): the same FastAPI process,
the same SQLite truth, the same LAN-trust model. Connect with::

    claude mcp add --transport http http://<pi>:8000/mcp

The tool surface is **read-mostly plus safe verbs**. Deliberately absent, by
design and forever: bias-tee control in any form, device-profile edits,
capture-settings changes, and all delete verbs — the guardrail is structural,
not behavioral (plan §12.4, CLAUDE.md safety invariants).
"""

from __future__ import annotations

from jansky_observe.mcp.server import build_mcp, mount_mcp

__all__ = ["build_mcp", "mount_mcp"]
