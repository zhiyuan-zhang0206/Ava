"""Ava Guide entry — /api/guide/draft.

Hands a natural-language operations request to an ava-guide agent: spawns an
agent that loads the ROOT `ava.skills.ava_guide` skill (the map for operating the
cluster via the `ava` CLI — start/update/track switching, MCP servers, installing
skills/plugins, presets, schedules) and returns its id so the Control page can
open the conversation. Mirrors the schedule/preset draft endpoints — a fixed
prompt pointed at the skill + `create_and_launch_agent`; the operator finishes
the task in the spawned agent's session.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from gateway.routers.agents import create_and_launch_agent
from ops.rpc_schemas import SpawnAgentRequest
from shared.machine import machine_name

router = APIRouter()


class GuideDraftRequest(BaseModel):
    nl: str = Field(description="Natural-language operations request for the guide agent.")


class GuideDraftResponse(BaseModel):
    agent_id: int


@router.post("/api/guide/draft")
async def draft_guide(body: GuideDraftRequest, request: Request) -> GuideDraftResponse:
    """Spawn an ava-guide agent for a natural-language ops request and return its
    id so the UI can open the conversation."""
    prompt = (
        "You are an Ava operations assistant. Read and follow ava.skills.ava_guide "
        "to help with this request — operate the cluster via the `ava` CLI (run / "
        "update / switch tracks, manage MCP servers, install skills or plugins, "
        "manage presets and schedules). Request:\n\n" + body.nl
    )
    body_obj = SpawnAgentRequest(
        spawner="user",
        prompt=prompt,
        prompt_source="user",
        label="ava-guide",
    )
    spawned = await create_and_launch_agent(body_obj, machine_name(), request.app.state.db_pool)
    return GuideDraftResponse(agent_id=spawned.id)
