"""jansky-observe — observation management for the Discovery Dish station.

Plan, run, and record attended radio observations end to end on the
KrakenRF Discovery Dish 700 mm + H-line feed (1420 MHz) → Airspy Mini →
Raspberry Pi 5 station. Two tiers: a Python server (FastAPI + a separate
SDR-owning capture daemon) and a thin browser UI.

Layers
------
- ``capture``   — the SDR-owning daemon: sources, DSP, ZeroMQ frame publisher
- ``server``    — FastAPI app: REST, WebSocket live view, browser UI
- ``frames``    — spectral-frame wire formats shared by daemon and server
- ``synthetic`` — synthetic noise + fake-HI generators (M0 walking skeleton, tests)
- ``config``    — runtime settings from environment variables

Sibling of the `jansky` course (whose library helpers this reuses) and
`jansky-research`. The full project plan lives in ``plans/jansky_observe.md``.
"""

from __future__ import annotations

__version__ = "0.13.0"
