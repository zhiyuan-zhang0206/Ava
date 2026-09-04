"""Composer commands endpoint — `GET /api/commands`.

Lists the registered prompt templates (see `ava._commands`) for the web
Composer's `/`-autocomplete. Read-only filesystem scan; no auth beyond the
gateway's default session gate. Only the metadata the dropdown needs is
returned — the body is never sent to the browser, since expansion happens
server-side in the agent's claim node (`ava._commands.expand_command`).
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Request

from gateway.schemas import CommandItem
from ops import cluster_rpc as _cluster_rpc

router = APIRouter()
_log = logging.getLogger(__name__)
_AGENT_SKILL_VIEW_TIMEOUT_S = 3.0


def _local_commands() -> list[CommandItem]:
    """The gateway-local fallback and backwards-compatible no-agent view."""
    from ava._commands import discover_commands

    return [
        CommandItem(
            name=c["name"],
            description=c["description"],
            instruction_hint=c["instruction_hint"],
        )
        for c in discover_commands()
    ]


def _agent_machine(request: Request, agent_id: int) -> str | None:
    """Machine recorded for an agent, or None when the row is absent."""
    with request.app.state.db_pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT machine FROM agents_meta WHERE id = %s", (agent_id,))
        row = cur.fetchone()
    return str(row[0]) if row is not None and row[0] is not None else None


@router.get("/api/commands")
async def get_commands(request: Request, agent_id: int | None = None) -> list[CommandItem]:
    """Composer commands, globally by default or as one agent sees them.

    An agent view always dials the machine recorded on ``agents_meta`` (including
    the gateway's own machine).  A missing/offline/version-skewed runner falls
    back to the historical gateway-local catalog so autocomplete remains usable.
    """
    if agent_id is None:
        return _local_commands()

    machine = await asyncio.to_thread(_agent_machine, request, agent_id)
    if machine is None:
        _log.warning("commands: agent %s has no agents_meta row; using local fallback", agent_id)
        return _local_commands()
    try:
        result = await _cluster_rpc.dispatch_to_machine(
            machine,
            "agent_skill_view",
            {"agent_id": agent_id},
            timeout_s=_AGENT_SKILL_VIEW_TIMEOUT_S,
        )
    except (_cluster_rpc.ClusterOpUnreachable, _cluster_rpc.ClusterOpFailed) as exc:
        _log.warning(
            "commands: agent %s command view unavailable on %s; using local fallback: %s",
            agent_id,
            machine,
            exc,
        )
        return _local_commands()
    return [CommandItem.model_validate(command) for command in result["commands"]]
