"""FastAPI server tier: REST, WebSocket live fan-out, and the browser UI.

Subscribes to the capture daemon's ZeroMQ PUB stream of spectral frames
(:mod:`jansky_observe.frames`), keeps the latest frame, and fans frames out
to browser WebSocket clients that render a live waterfall + spectrum.
"""

from __future__ import annotations

__all__: list[str] = []
