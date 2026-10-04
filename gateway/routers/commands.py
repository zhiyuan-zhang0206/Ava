"""Composer commands endpoint — `GET /api/commands`.

Lists the registered prompt templates (see `ava.composer_commands`) for the web
Composer's `/`-autocomplete. Read-only filesystem scan; no auth beyond the
gateway's default session gate. Only the metadata the dropdown needs is
returned — the body is never sent to the browser, since expansion happens
server-side in the agent's claim node (`ava.composer_commands.expand_command`).
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Request

from gateway.schemas.commands import CommandItem
from ops import cluster_rpc as _cluster_rpc
from ops.deploy_window import deploy_in_flight

router = APIRouter()
_log = logging.getLogger(__name__)
_AGENT_SKILL_VIEW_TIMEOUT_S = 3.0


def _local_commands() -> list[CommandItem]:
    """The gateway-local catalog: the unscoped view and the availability fallback."""
    from ava.composer_commands import discover_commands

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
    An open deploy window (read per request, task #4986) explains the
    unreachability — the fallback report drops to INFO for that request.
    """
    if agent_id is None:
        return _local_commands()

    machine = await asyncio.to_thread(_agent_machine, request, agent_id)
    if machine is None:
        _log.warning("commands: agent %s has no agents_meta row; using local fallback", agent_id)
        return _local_commands()
    window = await asyncio.to_thread(deploy_in_flight, request.app.state.db)
    try:
        result = await _cluster_rpc.dispatch_to_machine(
            request.app.state.db,
            machine,
            "agent_skill_view",
            {"agent_id": agent_id},
            timeout_s=_AGENT_SKILL_VIEW_TIMEOUT_S,
            quiet_unreachable=bool(window),
        )
    except (_cluster_rpc.ClusterOpUnreachable, _cluster_rpc.ClusterOpFailed) as exc:
        if window:
            _log.info(
                "commands: agent %s command view unavailable on %s — a deploy window is open "
                "(%s); using local fallback",
                agent_id,
                machine,
                window.detail,
            )
        else:
            _log.warning(
                "commands: agent %s command view unavailable on %s; using local fallback: %s",
                agent_id,
                machine,
                exc,
            )
        return _local_commands()
    return [CommandItem.model_validate(command) for command in result["commands"]]
