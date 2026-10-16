from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from gateway.agents.creation import create_and_launch_agent, guarded_draft_key
from gateway.http.model_catalog import require_model_catalog
from ops.rpc_schemas import SpawnAgentRequest

router = APIRouter()


class GuideDraftRequest(BaseModel):
    nl: str = Field(description="Natural-language operations request for the guide agent.")


class GuideDraftResponse(BaseModel):
    agent_id: int


@router.post(
    "/api/keyed/v1/guide/draft",
    description="Spawn an ava-guide agent for a natural-language ops request and return its\nid so the UI can open the conversation. Replays one raw draft intent and its\noriginal birth.",
)
async def guarded_draft_guide(
    body: GuideDraftRequest, request: Request, key: str = Depends(guarded_draft_key)
) -> GuideDraftResponse:

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
    spawned = await create_and_launch_agent(
        body_obj,
        request.app.state.machine.name(),
        request.app.state.db_pool,
        request.app.state.db,
        request.app.state.bus,
        catalog=require_model_catalog(request),
        authority=request.app.state.config_authority,
        creation_key=key,
        creation_identity=body.model_dump(mode="json"),
    )
    return GuideDraftResponse(agent_id=spawned.id)
