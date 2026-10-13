"""Agent lifecycle entry surface: the cross-path invariants + the status lookups.

Gateway-internal — agent processes no longer import this module. The agent SDK
(`ava.agents.*`) calls the gateway over HTTP. `resurrect` remains reachable here
as an internal op used by `resurrect_if_terminated` (no dedicated endpoint).

Agent birth (`create_agent_row`) lives in `base.agents.birth`; the row it creates is
validated and woken by `ops.lifecycle`. This package door re-exports the entry points
from their owning submodules:

- `wake` resurrects a terminated hosted incarnation (`resurrect_agent`) after
  its original lifecycle command settles; `resurrection_retry` holds the
  placement and termination boundaries a queued resurrection crosses.

One durable agent identity is served by its home agent-host. Spawn and
resurrection commit native intent and messages before publishing a wake;
admission binds the next turn to the host owner and a new generation.
"""

from __future__ import annotations

from base.api_contracts.agent_contract import AgentNotFound, AgentStatus
from base.db import Database
from ops.agents.wake import (
    resurrect_agent as resurrect_agent,
)


def get_agent_status(db: Database, agent_id: int) -> AgentStatus:
    """Look up the agent's current status.

    Raises:
        AgentNotFound: agent_id does not exist in agents_meta.
    """
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT status FROM agents_meta WHERE id = %s", (agent_id,))
        row = cur.fetchone()
    if row is None:
        raise AgentNotFound(f"agent {agent_id} does not exist")
    return AgentStatus(row[0])


def get_agent_machine(db: Database, agent_id: int) -> str:
    """Look up the agent's home machine (`agents_meta.machine`) — the host its
    process must run on (the boot placement gate rejects any other host).

    Raises:
        AgentNotFound: agent_id does not exist in agents_meta.
    """
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT machine FROM agents_meta WHERE id = %s", (agent_id,))
        row = cur.fetchone()
    if row is None:
        raise AgentNotFound(f"agent {agent_id} does not exist")
    return row[0]
