"""Revive a stale NON-terminated row back into a running process.

The boot half of the wake family (Task #1999 split out of `ops/agent_wake.py`,
which keeps resurrect / respawn and the resurrect claim machinery):
`revive_agent` wakes a `running`/`idling` row behind a dead pid. It shares the
same shape as resurrect — a CAS into unclaimed `idling`, then a relaunch
attached to the same `agent_id` (LangGraph's checkpointer restores the history,
so the process resumes rather than starts over) — and is deliberately
INVISIBLE to the agent: no lifecycle inbound, no marker, so a woken agent
cannot tell it was revived.

Hosted mode (`AVA_RUNNER_MODE=hosted`): the flip IS the whole op — no process
to launch. The dispatcher owns delivery, so the hosted branch publishes the
Redis wake instead and skips the launch machinery (defensive: the hosted
restarter is gated off, but a stray stale row must wake, never fork).

The caller MUST run this on the agent's home machine (`agents_meta.machine`) —
launching on any other host trips the boot placement gate and crash-loops
(agent 1513 incident). The restarter reaper satisfies this by only scanning
`machine = local`.
"""

from __future__ import annotations

from ops import agent_launch, runner_mode
from shared.agents import AgentStatus
from shared.db import fetch_one, publish_inbound_wake
from shared.db_transaction import write_transaction
from shared.live_announce import publish_agent_updated_sync
from shared.log import logger


def revive_agent(agent_id: int, dead_pid: int) -> bool:
    """Revive a dead 'running'/'idling' row: CAS to unclaimed 'idling' + launch a fresh
    process, with NO lifecycle inbound.

    The boot-revive half of Task #689 G5. A machine reboot / power-off leaves
    every local agent row 'running'/'idling' behind a dead (or recycled) pid;
    the restarter reaper used to force those rows to 'terminated', and nothing
    ever brought the agents back (crash-resurrect needs a pending inbound, the
    heartbeat only targets idling) -- the user-visible failure "the machine
    came back but the fleet stayed dead". The woken process finds its
    checkpoint and whatever inbound waited, no marker — but the CAS re-asserts
    the probed dead pid so a row whose process is actually alive (or already
    revived by a concurrent pass) is never double-launched.

    The caller MUST run this on the agent's home machine (`agents_meta.machine`)
    -- launching on any other host trips the boot placement gate and crash-loops
    (agent 1513 incident). The reaper satisfies this by only scanning
    `machine = local`.

    Crash-loop bound: if the revived process dies again at boot, the row lands
    as a boot-phase death and the reapers + launch-confirm force it to
    'terminated' -- at most one extra revive cycle per dead agent.

    Returns:
        True: won the CAS, new process launched.
        False: lost the race / the row is not 'running'/'idling' with that pid --
            noop, does not raise.
    """
    with write_transaction() as conn, conn.cursor() as cur:
        # Race-safe gate, pid-reasserted (ABA-closed like the reaper): flip +
        # commit so a concurrent revive/reap/launch sees unclaimed 'idling' and loses.
        # Clears pid/started_at -- the probed pid is a corpse (or recycled), it
        # must not linger as ghost data (agent 44 incident).
        cur.execute(
            "UPDATE agents_meta SET status = %s, pid = NULL, started_at = NULL, "
            "lease_expires_at = NULL "
            "WHERE id = %s AND status IN ('running', 'idling') AND pid = %s",
            (AgentStatus.IDLING, agent_id, dead_pid),
        )
        won_race = cur.rowcount == 1
        conn.commit()
        if not won_race:
            return False
        cur.execute(
            "SELECT config_overlay, birth_config FROM agents_meta WHERE id = %s", (agent_id,)
        )
        config_overlay: dict[str, object] | None
        birth_config: dict[str, object] | None
        config_overlay, birth_config = fetch_one(cur, "revive: read per-agent config")
        publish_agent_updated_sync(conn, agent_id)
    if runner_mode.is_hosted():
        # Hosted has no pid for a reaper to probe, so this branch is defensive —
        # but the hosted answer to a stale row is the same as swap-in's: flip to
        # 'idling', publish the wake, let the dispatcher materialize the turn.
        publish_inbound_wake(agent_id, "0")
        logger.info(
            "agent {agent_id} revived in hosted mode — wake published, no process launched",
            event="agent_revived",
            agent_id=agent_id,
            dead_pid=dead_pid,
        )
        return True
    agent_launch._require_released_agent_session(agent_id)
    agent_launch._launch_or_force_terminated(
        agent_id, config_overlay=config_overlay, birth_config=birth_config
    )
    logger.info(
        "agent {agent_id} revived (dead pid {dead_pid})",
        event="agent_revived",
        agent_id=agent_id,
        dead_pid=dead_pid,
    )
    return True
