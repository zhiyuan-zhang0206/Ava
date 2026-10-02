"""Agent lifecycle entry surface: the cross-path invariants + the status lookups.

Gateway-internal — agent processes no longer import this module. The agent SDK
(`ava.agents.*`) calls the gateway over HTTP. `resurrect` remains reachable here
as an internal op used by `resurrect_if_terminated` (no dedicated endpoint).

This package door re-exports the entry points from their owning submodules:

- `spawn` creates a new metadata row (`create_agent_row`), optionally forked
  from another agent's checkpoint (`latest_checkpoint_id`).
- `wake` resurrects a terminated hosted incarnation (`resurrect_agent`) after
  its original lifecycle command settles; `resurrection_retry` holds the
  placement and termination boundaries a queued resurrection crosses.

One durable agent identity is served by its home agent-host. Spawn and
resurrection commit native intent and messages before publishing a wake;
admission binds the next turn to the host owner and a new generation.
"""

from __future__ import annotations

import base.db
from base.agents import AgentNotFound, AgentStatus
from ops.agents.spawn import (
    _SPAWNER_AGENT_RE as _SPAWNER_AGENT_RE,
)
from ops.agents.spawn import (
    _spawner_agent_id_malformed as _spawner_agent_id_malformed,
)
from ops.agents.spawn import (
    create_agent_row as create_agent_row,
)
from ops.agents.spawn import (
    latest_checkpoint_id as latest_checkpoint_id,
)
from ops.agents.wake import (
    resurrect_agent as resurrect_agent,
)


def get_agent_status(agent_id: int) -> AgentStatus:
    """Look up the agent's current status.

    Raises:
        AgentNotFound: agent_id does not exist in agents_meta.
    """
    with base.db.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT status FROM agents_meta WHERE id = %s", (agent_id,))
        row = cur.fetchone()
    if row is None:
        raise AgentNotFound(f"agent {agent_id} does not exist")
    return AgentStatus(row[0])


def get_agent_machine(agent_id: int) -> str:
    """Look up the agent's home machine (`agents_meta.machine`) — the host its
    process must run on (the boot placement gate rejects any other host).

    Raises:
        AgentNotFound: agent_id does not exist in agents_meta.
    """
    with base.db.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT machine FROM agents_meta WHERE id = %s", (agent_id,))
        row = cur.fetchone()
    if row is None:
        raise AgentNotFound(f"agent {agent_id} does not exist")
    return row[0]
