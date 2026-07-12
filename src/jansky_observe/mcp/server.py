"""FastMCP tool definitions proxying the app's own JSON API.

Every tool goes through the same routes the browser uses (via an in-process
ASGI client) or the same modules the routes call — one truth, no duplicated
logic. See the package docstring for what is deliberately absent.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
from fastapi import FastAPI
from fastmcp import FastMCP
from sqlmodel import Session, select
from starlette.applications import Starlette

from jansky_observe.models import Location, ObservationType, Observer, RadioSource
from jansky_observe.weather.provider import get_weather as fetch_weather

__all__ = ["build_mcp", "mount_mcp"]


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://station")


async def _get(app: FastAPI, path: str, **params: Any) -> Any:
    async with _client(app) as client:
        response = await client.get(path, params={k: v for k, v in params.items() if v is not None})
        response.raise_for_status()
        return response.json()


async def _post_json(app: FastAPI, path: str, body: dict[str, Any]) -> Any:
    async with _client(app) as client:
        response = await client.post(path, json=body)
        response.raise_for_status()
        return response.json()


def build_mcp(app: FastAPI) -> FastMCP:
    """Build the station's FastMCP server bound to ``app``."""
    mcp: FastMCP = FastMCP(
        name="jansky-observe",
        instructions=(
            "Observation management for the Discovery Dish hydrogen-line station. "
            "Read tools + safe verbs only; there is no bias-tee control and no delete "
            "verb, by design. Checklist ticks you perform are recorded as by='claude'."
        ),
    )

    @mcp.tool
    async def whats_up(window_h: int = 8) -> list[dict[str, Any]]:
        """Every catalog source's current az/el (station pointing offsets applied),
        drift rate, transit time/elevation, beam-crossing minutes, and rise/set,
        sorted by elevation. window_h flags transits within the next N hours."""
        return await _get(app, "/api/whats_up", window_h=window_h)

    @mcp.tool
    async def get_pointing(source_id: int) -> dict[str, Any]:
        """Current pointing for one source: az/el with the station's Sun-cal
        offsets applied (raw values included), drift °/min, transit, beam-crossing."""
        return await _get(app, f"/api/pointing/{source_id}")

    @mcp.tool
    async def list_observations(status: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        """Recent observations, newest first. Optionally filter by status
        (planned | running | done | aborted)."""
        rows: list[dict[str, Any]] = await _get(app, "/api/observations")
        if status is not None:
            rows = [r for r in rows if r.get("status") == status]
        return rows[:limit]

    @mcp.tool
    async def get_observation(observation_id: int) -> dict[str, Any]:
        """One observation in full: metadata, pointing record, weather snapshot,
        checklist states (who/when), notes, and captures."""
        return await _get(app, f"/api/observations/{observation_id}")

    @mcp.tool
    async def get_weather() -> dict[str, Any]:
        """Current conditions + 3-hour forecast at the default (Home) location,
        from NWS with Open-Meteo fallback."""
        engine = app.state.engine
        with Session(engine) as session:
            home = session.exec(
                select(Location).where(Location.is_default == True)  # noqa: E712
            ).first()
            if home is None:
                raise ValueError("no default location configured")
            lat, lon = home.lat_deg, home.lon_deg
        return await asyncio.to_thread(fetch_weather, lat, lon)

    @mcp.tool
    async def get_live_status() -> dict[str, Any]:
        """The capture daemon's live status: source, capture state, disk free,
        projected rates, sticky overrun flag."""
        return await _get(app, "/api/capture/status")

    @mcp.tool
    async def create_observation_draft(
        observation_type: str,
        source: str,
        name: str = "",
        observers: list[str] | None = None,
    ) -> dict[str, Any]:
        """Create a planned observation (the wizard pre-fill): resolves the type,
        source, and observer names, materializes the type's checklist, and returns
        the draft. Ticks nothing — the checklist belongs to the human at the dish."""
        engine = app.state.engine
        with Session(engine) as session:
            obs_type = session.exec(
                select(ObservationType).where(ObservationType.name == observation_type)
            ).first()
            radio_source = session.exec(
                select(RadioSource).where(RadioSource.name == source)
            ).first()
            home = session.exec(
                select(Location).where(Location.is_default == True)  # noqa: E712
            ).first()
            if obs_type is None:
                known = session.exec(select(ObservationType.name)).all()
                raise ValueError(f"unknown observation type {observation_type!r}; known: {known}")
            if radio_source is None:
                known = session.exec(select(RadioSource.name)).all()
                raise ValueError(f"unknown source {source!r}; known: {known}")
            if home is None:
                raise ValueError("no default location configured")
            observer_ids: list[int] = []
            for observer_name in observers or []:
                observer = session.exec(
                    select(Observer).where(Observer.name == observer_name)
                ).first()
                if observer is None or observer.id is None:
                    raise ValueError(f"unknown observer {observer_name!r}")
                observer_ids.append(observer.id)
            form: dict[str, Any] = {
                "observation_type_id": obs_type.id,
                "location_id": home.id,
                "source_id": radio_source.id,
                "name": name,
                "observer_ids": observer_ids,
            }
        async with _client(app) as client:
            response = await client.post("/wizard/create", data=form)
            if response.status_code != 303:  # success = redirect to step 3
                response.raise_for_status()
                raise ValueError(f"unexpected wizard response {response.status_code}")
            # 303 → /wizard/{id}/step3; the id is the second path segment.
            obs_id = int(response.headers["location"].split("/")[2])
        return await _get(app, f"/api/observations/{obs_id}")

    @mcp.tool
    async def append_note(observation_id: int, text: str) -> dict[str, Any]:
        """Append a timestamped paragraph to an observation's notes. Interpretation
        lives in notes; verdicts only ever come from the deterministic classifiers."""
        return await _post_json(app, f"/api/observations/{observation_id}/notes", {"text": text})

    @mcp.tool
    async def tick_checklist_item(
        observation_id: int, state_id: int, by: str = "claude"
    ) -> dict[str, Any]:
        """Tick one checklist item, recording who (default 'claude') and when."""
        return await _post_json(
            app, f"/api/observations/{observation_id}/checklist/{state_id}/tick", {"by": by}
        )

    return mcp


def mount_mcp(app: FastAPI) -> Starlette:
    """Mount the MCP server at ``/mcp``; returns the sub-app for lifespan wiring.

    The returned Starlette app's lifespan MUST be entered by the parent app's
    lifespan (FastMCP's HTTP transport runs a session manager) — see
    ``server/app.py``.
    """
    mcp = build_mcp(app)
    mcp_app = mcp.http_app(path="/")
    app.mount("/mcp", mcp_app)
    return mcp_app
