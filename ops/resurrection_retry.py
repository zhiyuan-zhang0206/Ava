"""Placement and termination boundaries for a queued resurrection."""

import psycopg

from shared.agents import AgentNotFound, MachinePaused, ResurrectError


class ResurrectExitDeferredError(ResurrectError):
    """The old execution entity has not yet been positively observed ended."""


class ResurrectTriggerStaleError(ResurrectError):
    """The exact pending wake no longer qualifies; the local op returns a no-op."""


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
