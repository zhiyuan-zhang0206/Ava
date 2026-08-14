"""Agent lifecycle entry surface: the cross-path invariants + the status lookups.

Gateway-internal — agent processes no longer import this module. The agent SDK
(`ava.agents.*`) calls the gateway over HTTP. `resurrect` remains reachable here
as an internal op used by `resurrect_if_terminated` (no dedicated endpoint).

The two halves of the lifecycle live beside this module and are re-exported from
it, so every existing `from ops.agents import ...` keeps working:

- `ops/agent_spawn.py` — **birth**: a new agents_meta row (`create_agent_row`, gateway-side),
  optionally forked from another agent's checkpoint (`latest_checkpoint_id`).
- `ops/agent_wake.py` — **wake**: an existing row back into a running process
  (`resurrect_agent` / `respawn_agent` / `swap_in_agent`).

Either way the *mechanics* of actually launching a detached native child
process and confirming it came up live in `ops/agent_launch.py`
(`_launch_agent_process` / `_launch_or_force_terminated` / `_kill_stale_session`).

Agent process / agents row is 1:1 by `agent_id`.

After the process starts, it itself executes `UPDATE agents_meta SET
status='starting', pid=..., started_at=... WHERE id=N AND status='allocated'`
— 0 rows raises (prevents a race spawning two processes contending for the
same id). 'starting' marks "process is up but still initializing" (import /
setup llm / db / redis); once it enters the graph, the claim_node's top
`leave_starting_state` promotes to 'running'. After a successful spawn,
`_launch_agent_process` polls `agents_meta.status` to confirm this UPDATE
actually happened; on timeout it raises, avoiding "launched but child crash"
leaving the row permanently stuck in 'allocated' (agent 137 / 44 incident). The
poll's deadline is load-tolerant — a child whose process is still alive at the
deadline gets one bounded extension rather than having its row taken away
mid-boot.

`pid` + `started_at` are only set during 'starting' / 'running' / 'idling'.
Spawn / resurrect / respawn, when switching status back to 'allocated',
**simultaneously** UPDATE pid=NULL, started_at=NULL — otherwise the previous
running session's fields become ghost data misleading operators.
`enter_starting_state` re-fills these two columns when it UPDATEs
status='starting'.

Two cleanup paths on launch failure:
- spawn: leave row in 'allocated' and re-raise — gives operators a way to
  diagnose "why did it never start" (a weekly cleanup task's concern). This
  avoids the non-atomic "INSERT first then launch" failure erasing the thread
  history.
- resurrect / respawn: `_launch_or_force_terminated` forces status to
  'terminated' and re-raises — the agent already existed; the operator cares
  about "did the wake succeed", and failure lets the caller retry resurrect
  (re-run starting from 'terminated').
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
from ops.agent_wake import (
    swap_in_agent as swap_in_agent,
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
