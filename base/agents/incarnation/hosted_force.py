"""Hosted force acceptance and original-host quiescence on the existing command.

Only the live host's serialized turn pump may observe completion, after its real
continuation and owned exec cleanup return. Lease expiry, an empty cache, and
the host process dying are not proof that independent exec children ended, so
nothing settles an unobserved force on their strength: if the original host
dies first, the lifecycle pointer stays set and the stuck-lifecycle-pointer
scan (`services/upkeep/ttl_reaper/lifecycle_fences.py`) alerts an operator.
"""

import asyncio
from collections.abc import Callable
from uuid import UUID

import psycopg
from psycopg import sql
from psycopg_pool import AsyncConnectionPool

from base.agents.incarnation.lifecycle_acceptance import COMMAND_KILLS_SHELL_SESSIONS
from base.db.transaction import async_write_transaction
from base.native_process.runtime_incarnation import RuntimeIncarnation


def install_hosted_force(conn: psycopg.Connection, agent_id: int, command_id: int) -> None:
    """Attach force to the current hosted incarnation under the caller's row lock."""
    row = conn.execute(
        "SELECT runtime_generation,runtime_owner FROM agents_meta WHERE id=%s "
        "AND runtime_kind='hosted' AND runtime_generation IS NOT NULL "
        "AND runtime_owner IS NOT NULL FOR UPDATE",
        (agent_id,),
    ).fetchone()
    if row is None:
        return
    command = conn.execute(
        "UPDATE inbound_messages SET target_generation=%s,target_owner=%s,"
        "status='claimed',claimed_at=clock_timestamp(),applied_at=clock_timestamp() "
        "WHERE id=%s AND agent_id=%s AND kind='terminate' AND status='pending'",
        (row[0], row[1], command_id, agent_id),
    )
    if command.rowcount != 1:
        raise RuntimeError("hosted force lost its newly inserted command")
    conn.execute(
        "UPDATE agents_meta SET lifecycle_command_id=%s WHERE id=%s",
        (command_id, agent_id),
    )
    from base.agents.incarnation.resources import freeze_resources

    resources = conn.execute(
        "SELECT incarnation_resources FROM agents_meta WHERE id=%s", (agent_id,)
    ).fetchone()
    if resources is not None and resources[0] is not None:
        freeze_resources(conn, RuntimeIncarnation(agent_id, row[0], row[1]), command_id)


async def original_host_force(
    pool: AsyncConnectionPool,
    agent_id: int,
    owner: UUID,
    machine: str,
    *,
    command_id: int | None = None,
    quiescent: bool = False,
    kill_shell_sessions: Callable[[int, int], None] | None = None,
) -> bool:
    """Validate the exact force, optionally settle in the original serialized pump.

    ``quiescent`` is an internal callsite contract, never accepted from HTTP or a
    payload: the host pump still excludes a replacement and its awaited work ended.
    The command's fixed target is checked against that host's actual boot owner.

    A force that asked to kill the agent's shell sessions requested an immediate
    attempt at acceptance, but a step still draining could create one afterwards;
    settlement captures one new shell cutoff under the validated row lock.
    After recording the observation and committing, it attempts older sessions
    once. Cleanup failure cannot undo the observation; late sessions and crash
    leftovers belong to operator cleanup.
    """
    async with async_write_transaction(pool) as conn:
        row = await (
            await conn.execute(
                "SELECT runtime_generation,lifecycle_command_id,session_index FROM agents_meta "
                "WHERE id=%s AND runtime_owner=%s AND machine=%s "
                "AND runtime_kind='hosted' AND status='terminated' FOR UPDATE",
                (agent_id, owner, machine),
            )
        ).fetchone()
        if row is None or row[0] is None or row[1] is None:
            return False
        if command_id is not None and row[1] != command_id:
            return False
        command = await (
            await conn.execute(
                sql.SQL(
                    "SELECT id, {} FROM inbound_messages WHERE id=%s AND agent_id=%s "
                    "AND kind='terminate' AND status='claimed' AND applied_at IS NOT NULL "
                    "AND observed_at IS NULL AND target_generation=%s AND target_owner=%s "
                    "FOR UPDATE"
                ).format(sql.SQL(COMMAND_KILLS_SHELL_SESSIONS)),
                (row[1], agent_id, row[0], owner),
            )
        ).fetchone()
        if command is None:
            return False
        if quiescent:
            from base.agents.incarnation.resource_admission import require_resources_closed_async

            await require_resources_closed_async(conn, agent_id)
            _require_shell_killer(command[1], kill_shell_sessions)
            await conn.execute(
                "UPDATE inbound_messages SET observed_at=clock_timestamp(),status='done' "
                "WHERE id=%s",
                (row[1],),
            )
            await conn.execute(
                "UPDATE agents_meta SET lifecycle_command_id=NULL,lease_expires_at=NULL "
                "WHERE id=%s AND lifecycle_command_id=%s AND runtime_owner=%s "
                "AND runtime_generation=%s",
                (agent_id, row[1], owner, row[0]),
            )
    if quiescent:
        await _sweep_requested_shell_kill(agent_id, row[2], command[1], kill_shell_sessions)
    return True


async def _sweep_requested_shell_kill(
    agent_id: int,
    cutoff: int,
    requested: object,
    kill_shell_sessions: Callable[[int, int], None] | None,
) -> None:
    """Kill the agent's shell sessions again when its force asked for it.

    Runs after the settlement commits, with the cutoff captured under its
    validated row lock. The kill is idempotent. The killer belongs
    to the host (the base layer does not reach the ops session primitives)
    and reports failures; a caller that binds none may not settle such a force.
    """
    if requested is not True:
        return
    if kill_shell_sessions is not None:
        await asyncio.to_thread(kill_shell_sessions, agent_id, cutoff)


def _require_shell_killer(requested: object, killer: Callable[[int, int], None] | None) -> None:
    """Refuse a misconfigured owner before committing its observation."""
    if requested is True and killer is None:
        raise RuntimeError("shell-session kill requested but no killer is bound")
