"""Package install entry — /api/packages/draft.

Hands a natural-language "I want a capability like X" request to an
ava-package-installer agent: spawns an agent that loads
`ava.skills.ava_package_installer` and returns its id so the Control page can
open the conversation. Mirrors the schedule / guide draft endpoints — a fixed
prompt pointed at the skill + `create_and_launch_agent`; the user finishes the
task in the spawned agent's session.

The whole install lifecycle (clarify -> discover candidates -> confirm ->
install -> spawn a test agent -> read and judge the package -> report what
adaptation it needs) lives in the skill. This endpoint only routes: it carries
which *kind* of package the entry point was for and the user's words. There is
deliberately no URL/spec field — the user is not expected to know which package
is any good, so every install goes through an agent that can look.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from gateway.routers.agents import create_and_launch_agent
from ops.rpc_schemas import SpawnAgentRequest
from shared.machine import machine_name

router = APIRouter()

PackageKind = Literal["skill", "plugin", "mcp"]

# Per-kind framing handed to the agent: what the thing is, and how much trust
# installing it costs. The skill turns the second half into the confirm gate
# (a plugin/MCP runs code here, so the user confirms the candidate first).
_KIND_BRIEF: dict[str, str] = {
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


@router.post("/api/packages/draft")
async def draft_package(body: PackageDraftRequest, request: Request) -> PackageDraftResponse:
    """Spawn an ava-package-installer agent for a natural-language install request
    and return its id so the UI can open the conversation. 422 on an unknown kind."""
    prompt = (
        "You are installing a package for the user. Read and follow "
        "ava.skills.ava_package_installer and run the whole lifecycle it describes: "
        "clarify what is actually wanted, find candidates, confirm with the user "
        "before installing anything that runs code, install, spawn a test agent to "
        "verify it works, read the package to judge whether it is any good, and "
        "report back — including what adaptation it needs if it falls short.\n\n"
        f"Kind: {body.kind} — {_KIND_BRIEF[body.kind]}\n\n"
        f"Request:\n\n{body.nl}"
    )
    body_obj = SpawnAgentRequest(
        spawner="user",
        prompt=prompt,
        prompt_source="user",
        label="ava-package-installer",
    )
    spawned = await create_and_launch_agent(body_obj, machine_name(), request.app.state.db_pool)
    return PackageDraftResponse(agent_id=spawned.id)
