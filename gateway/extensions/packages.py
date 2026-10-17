from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from gateway.agents.creation import create_and_launch_agent, guarded_draft_key
from gateway.http.model_catalog import require_model_catalog
from ops.rpc_schemas import SpawnAgentRequest

router = APIRouter()

PackageKind = Literal["skill", "plugin", "mcp"]


KIND_BRIEF: dict[str, str] = {
    "skill": (
        "a skill — an instruction pack an agent reads and follows; installing it runs "
        "no code of its own"
    ),
    "plugin": (
        "a Claude Code plugin — it ships hooks / sub-agents / bundled MCP servers that "
        "RUN CODE in this environment"
    ),
    "mcp": ("an MCP server — an external tool server that RUNS AS A PROCESS in this environment"),
}


class PackageDraftRequest(BaseModel):
    kind: PackageKind = Field(description="Which package surface the request came from.")
    nl: str = Field(description="Natural-language description of the capability wanted.")


class PackageDraftResponse(BaseModel):
    agent_id: int


@router.post(
    "/api/keyed/v1/packages/draft",
    description="Spawn an meiva-package-installer agent for a natural-language install request\nand return its id so the UI can open the conversation. Replays one raw draft\nintent and its original birth; 422 on an unknown kind.",
)
async def guarded_draft_package(
    body: PackageDraftRequest, request: Request, key: str = Depends(guarded_draft_key)
) -> PackageDraftResponse:

    prompt = (
        "You are installing a package for the user. Read and follow "
        "meiva.skills.meiva_guide.packages.install and run the whole lifecycle it describes: "
        "clarify what is actually wanted, find candidates, confirm with the user "
        "before installing anything that runs code, install, spawn a test agent to "
        "verify it works, read the package to judge whether it is any good, and "
        "report back — including what adaptation it needs if it falls short.\n\n"
        f"Kind: {body.kind} — {KIND_BRIEF[body.kind]}\n\n"
        f"Request:\n\n{body.nl}"
    )
    body_obj = SpawnAgentRequest(
        spawner="user",
        prompt=prompt,
        prompt_source="user",
        label="meiva-package-installer",
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
    return PackageDraftResponse(agent_id=spawned.id)
