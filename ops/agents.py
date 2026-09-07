"""Agent lifecycle entry surface: the cross-path invariants + the status lookups.

Gateway-internal — agent processes no longer import this module. The agent SDK
(`ava.agents.*`) calls the gateway over HTTP. `resurrect` remains reachable here
as an internal op used by `resurrect_if_terminated` (no dedicated endpoint).

The two halves of the lifecycle live beside this module and are re-exported from
it, so every existing `from ops.agents import ...` keeps working:

- `ops/agent_spawn.py` — **birth**: a new agents_meta row (`create_agent_row`, gateway-side),
  optionally forked from another agent's checkpoint (`latest_checkpoint_id`).
- `ops/agent_wake.py` — **wake**: an existing row back into a running process
  (`resurrect_agent` / `respawn_agent`).

Either way the *mechanics* of actually launching a detached native child
process and confirming it came up live in `ops/agent_launch.py`
(`_launch_agent_process` / `_launch_or_force_terminated` / `_require_released_agent_session`).

One durable agent identity is served by its home agent-host. Spawn and
resurrection commit native intent and messages before publishing a wake;
admission binds the next turn to the host owner and a new generation.
"""

from __future__ import annotations

import shared.db
from ops.agent_spawn import (
    _SPAWNER_AGENT_RE as _SPAWNER_AGENT_RE,
)
from ops.agent_spawn import (
    _copy_checkpoint_chain as _copy_checkpoint_chain,
)
from ops.agent_spawn import (
    _spawner_agent_id_malformed as _spawner_agent_id_malformed,
)
from ops.agent_spawn import (
    create_agent_row as create_agent_row,
)
from ops.agent_spawn import (
    latest_checkpoint_id as latest_checkpoint_id,
)
from ops.agent_wake import (
    resurrect_agent as resurrect_agent,
)
from shared.agents import AgentNotFound, AgentStatus


def get_agent_status(agent_id: int) -> AgentStatus:
    """Look up the agent's current status.

    Raises:
        AgentNotFound: agent_id does not exist in agents_meta.
    """
    with shared.db.connect() as conn, conn.cursor() as cur:
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
    with shared.db.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT machine FROM agents_meta WHERE id = %s", (agent_id,))
        row = cur.fetchone()
    if row is None:
        raise AgentNotFound(f"agent {agent_id} does not exist")
    return row[0]
