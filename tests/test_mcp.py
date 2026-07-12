"""Tests for the MCP surface — in-memory FastMCP client against a seeded app."""

from __future__ import annotations

import asyncio
import json

from fastmcp import Client

from jansky_observe.config import Settings
from jansky_observe.db import init_db
from jansky_observe.mcp import build_mcp
from jansky_observe.server.app import create_app


def _app(tmp_path):
    engine = init_db(tmp_path)
    return create_app(Settings(data_dir=str(tmp_path)), engine=engine)


def _payload(result):
    return json.loads(result.content[0].text)


def test_tool_surface_has_no_forbidden_verbs(tmp_path):
    mcp = build_mcp(_app(tmp_path))

    async def names():
        async with Client(mcp) as client:
            return sorted(tool.name for tool in await client.list_tools())

    tools = asyncio.run(names())
    assert tools == [
        "append_note",
        "create_observation_draft",
        "get_live_status",
        "get_observation",
        "get_pointing",
        "get_weather",
        "list_observations",
        "tick_checklist_item",
        "whats_up",
    ]
    joined = " ".join(tools)
    assert "bias" not in joined and "delete" not in joined  # structural guardrail


def test_whats_up_and_pointing(tmp_path):
    mcp = build_mcp(_app(tmp_path))

    async def scenario():
        async with Client(mcp) as client:
            up = _payload(await client.call_tool("whats_up", {"window_h": 8}))
            assert len(up) >= 6  # all seeded sources
            assert all("el_deg" in row for row in up)
            first_id = up[0]["source_id"]
            pointing = _payload(await client.call_tool("get_pointing", {"source_id": first_id}))
            assert "az_deg" in pointing

    asyncio.run(scenario())


def test_draft_note_tick_round_trip(tmp_path):
    mcp = build_mcp(_app(tmp_path))

    async def scenario():
        async with Client(mcp) as client:
            draft = _payload(
                await client.call_tool(
                    "create_observation_draft",
                    {
                        "observation_type": "HI pointed — Cygnus region",
                        "source": "Cygnus region HI",
                        "name": "mcp draft",
                    },
                )
            )
            assert draft["status"] == "planned"
            assert draft["checklist"], "checklist template not materialized"
            assert all(not item["checked"] for item in draft["checklist"])
            obs_id = draft["id"]

            noted = _payload(
                await client.call_tool(
                    "append_note", {"observation_id": obs_id, "text": "planned via MCP"}
                )
            )
            assert "planned via MCP" in noted["notes"]

            state_id = draft["checklist"][0]["state_id"]
            ticked = _payload(
                await client.call_tool(
                    "tick_checklist_item", {"observation_id": obs_id, "state_id": state_id}
                )
            )
            assert ticked["checked"] and ticked["checked_by"] == "claude"
            refreshed = _payload(
                await client.call_tool("get_observation", {"observation_id": obs_id})
            )
            ticked_item = next(i for i in refreshed["checklist"] if i["state_id"] == state_id)
            assert ticked_item["checked"] and ticked_item["checked_by"] == "claude"

    asyncio.run(scenario())


def test_draft_unknown_type_names_known_types(tmp_path):
    mcp = build_mcp(_app(tmp_path))

    async def scenario():
        async with Client(mcp) as client:
            result = await client.call_tool(
                "create_observation_draft",
                {"observation_type": "nope", "source": "Sun"},
                raise_on_error=False,
            )
            assert result.is_error
            assert "Sun pointing calibration" in result.content[0].text

    asyncio.run(scenario())


def test_mcp_mounted_on_http_app(tmp_path):
    from fastapi.testclient import TestClient

    app = _app(tmp_path)
    with TestClient(app) as client:
        response = client.get("/mcp/")
        assert response.status_code != 404
