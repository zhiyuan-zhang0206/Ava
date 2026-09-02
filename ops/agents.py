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

Agent process / agents row is 1:1 by `agent_id`.

At process start, `agent._starting.claim_agent_row` executes one CAS from an
unclaimed idling row to `running`, writing pid, started_at, and the first lease.
The `status='idling' AND pid IS NULL` predicate admits exactly one process for
an agent id. `_launch_agent_process` confirms that the claim wrote a pid; a
timeout leaves an unclaimed row for the boot reaper rather than adding a second
status value for bootstrap.

Spawn, resurrect, and respawn clear pid, started_at, and lease before
launching a new child. The claim CAS re-fills those ownership columns atomically,
so a prior process's values never masquerade as the new child.

Two cleanup paths on launch failure:
- spawn: leave an unclaimed idling row and re-raise — gives operators a way to
  diagnose "why did it never start" (a weekly cleanup task's concern). This
  avoids the non-atomic "INSERT first then launch" failure erasing the thread
  history.
- resurrect / respawn: `_launch_or_force_terminated` forces status to
  'terminated' and re-raises — the agent already existed; the operator cares
  about "did the wake succeed", and failure lets the caller retry resurrect
  (re-run from 'terminated').
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
    respawn_agent as respawn_agent,
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
