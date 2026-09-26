"""Placement and termination boundaries for a queued resurrection."""

from uuid import UUID

import psycopg

from shared.agents import AgentNotFound, MachinePaused, ResurrectError, ResurrectRefused
from shared.runtime_incarnation import RuntimeIncarnation


class ResurrectSettlementDeferredError(ResurrectError):
    """The original hosted lifecycle command has not yet settled."""


class ResurrectTriggerStaleError(ResurrectError):
    """The exact pending wake no longer qualifies; the local op returns a no-op."""


def hosted_resurrection_target(
    agent_id: int,
    *,
    kind: str | None,
    generation: UUID | None,
    owner: UUID | None,
    pid: int | None,
) -> RuntimeIncarnation:
    """Require retained hosted authority; historical rows need explicit cutover."""
    if kind != "hosted" or generation is None or owner is None or pid is not None:
        raise ResurrectRefused("runtime_cutover_required")
    return RuntimeIncarnation(agent_id, generation, owner)


def lock_active_home_machine(cur: psycopg.Cursor, agent_id: int) -> str:
    """Share the pause latch before metadata/inbound locks or budget writes."""
    cur.execute("SELECT machine FROM agents_meta WHERE id = %s", (agent_id,))
    agent_row = cur.fetchone()
    if agent_row is None:
        raise AgentNotFound(f"agent {agent_id} does not exist")
    home_machine = agent_row[0]
    if not isinstance(home_machine, str):
        raise ResurrectTriggerStaleError("resurrection target has no registered placement")
    cur.execute("SELECT paused_at FROM machines WHERE name = %s FOR SHARE", (home_machine,))
    machine_row = cur.fetchone()
    if machine_row is not None and machine_row[0] is not None:
        raise MachinePaused(
            f"agent {agent_id} home machine {home_machine!r} is paused; "
            "resume it before resurrecting"
        )
    return home_machine
