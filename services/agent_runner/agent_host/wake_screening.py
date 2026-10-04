"""Screen a wake for a turn: the agent's stored row, and whether this host runs it.

Read once per turn, before admission (`AgentHost._run_turn`): the row carries the
machine, status and two config maps the turn needs, and the two quiet rejections
decide whether this host is the one to run it. Split out of `host.py` to keep it
inside the file-size ceiling.
"""

from __future__ import annotations

import psycopg
from psycopg_pool import AsyncConnectionPool

from base.log import logger
from services.agent_runner.agent_host.runtime import _StoredConfig

__all__ = ["_is_runnable", "_read_stored_config"]


def _is_runnable(machine: str, agent_id: int, stored: _StoredConfig) -> bool:
    """Whether the host at `machine` should hand `agent_id` a turn right now.

    Two rejections, deliberately quiet rather than WARNING: a foreign
    agent's wake is normal cross-talk (the dispatcher's pattern subscription
    is cluster-wide, so every runner sees every wake), and a terminated
    agent's wake is the delivery watchdog's resurrect path doing its job.
    Neither is a fault of this host.
    """
    if stored.machine != machine:
        logger.debug(
            "hosted wake for agent {agent_id} belongs to machine {owner} — not ours",
            agent_id=agent_id,
            owner=stored.machine,
        )
        return False
    if stored.status == "terminated":
        logger.info(
            "hosted wake for agent {agent_id} ignored — status {status} is not runnable",
            agent_id=agent_id,
            status=stored.status,
        )
        return False
    return True


async def _read_stored_config(
    control_pool: AsyncConnectionPool[psycopg.AsyncConnection], agent_id: int
) -> _StoredConfig | None:
    """This agent's machine, status and two config maps in one round trip.

    None = the row is gone, which is a real anomaly (a wake was published for
    an agent that does not exist) and says so, unlike the two ordinary
    rejections above.
    """
    async with control_pool.connection() as conn:
        row = await (
            await conn.execute(
                "SELECT machine, status, config_overlay, birth_config "
                "FROM agents_meta WHERE id = %s",
                (agent_id,),
            )
        ).fetchone()
    if row is None:
        logger.warning(
            "hosted wake for agent {agent_id} has no agents_meta row — ignoring",
            agent_id=agent_id,
        )
        return None
    return _StoredConfig(machine=row[0], status=row[1], config_overlay=row[2], birth_config=row[3])
