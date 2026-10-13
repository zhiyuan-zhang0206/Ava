"""Live-agent predicate and the fleet restart signal that drains it."""

from collections.abc import Collection
from datetime import UTC, datetime

from base.agents.impersonation.wake import publish_inbound_wake
from base.api_contracts.agent_contract import AgentStatus
from base.db import Database
from base.events.live.bus import EventBus

# A "live" agent is one currently holding a process (status running/idling) —
# the set a cluster-wide stop-the-world (quiesce before a schema migration) must
# drain. The three helpers below all key off this same predicate; the statuses
# live in ONE constant, passed as a parameter (`status = ANY(%s)`), so a "live"
# semantics change touches one line, not three literal copies (audit
# 05-gateway-lifecycle A3).
# R1 (Task #1021): the single "alive" predicate — status in {running, idling}
# AND lease unexpired. The lease (`agents_meta.lease_expires_at`, written at
# claim by `agent._starting.claim_agent_row`, renewed by the agent's loop)
# is the liveness authority: a process that died without writing 'terminated'
# leaves its status behind and the lease expires; a process that cannot renew
# (wedged, pre-lease code) is a zombie the reaper collects.
#
# The statuses live in ONE constant and the lease condition in ONE fragment
# (`ALIVE_SQL`), so a "live" semantics change touches one line, not the literal
# copies (audit 05-gateway-lifecycle A3 + r1-state-liveness).
ALIVE_STATUSES: tuple[str, ...] = (
    AgentStatus.RUNNING.value,
    AgentStatus.IDLING.value,
)

# The SQL half of the predicate. Every reader interpolates `status = ANY(%s)
# AND lease_expires_at > now()` (first parameter = list(ALIVE_STATUSES)) so
# "alive" stays one definition across queries of every shape.
ALIVE_SQL = "status = ANY(%s) AND lease_expires_at > now()"


def agent_is_alive(status: str | None, lease_expires_at: datetime | None) -> bool:
    """The Python half of the single alive predicate, for row-based checks.

    A row is alive iff its status says a process should own it AND its lease is
    unexpired. `None` lease (never granted — pre-lease code) reads as dead,
    matching the SQL fragment: the reaper collects such rows so they land on
    code that renews.
    """
    if status not in ALIVE_STATUSES:
        return False
    if lease_expires_at is None:
        return False
    return lease_expires_at > datetime.now(UTC)


def signal_live_agents_restart(
    db: Database,
    bus: EventBus,
    source: str,
    *,
    exclude_agent_ids: Collection[int] = (),
    machine: str | None = None,
) -> list[int]:
    """Bulk-INSERT one kind='restart' inbound per live agent, wake each over Redis; return ids.

    The set-based form of the per-agent restart path (gateway
    restart_agent_op -> insert_inbound_message(kind='restart')): same
    inbound_messages contract, one INSERT ... SELECT over every live agent, and
    the same per-agent Redis wake so an *idling* agent restarts now instead of
    stalling to its SELECT recheck. That prompt drain matters here specifically:
    this is the fleet update's quiesce step, whose convergence loop keeps signalling
    until every live agent has drained — a 30s recheck lag per idle agent would
    drag the whole quiesce out. `source` tags the signal's origin
    (e.g. 'system:update').

    `machine` scopes the signal to one host's agents — the per-host quiesce the
    agent-runner self-update runs before it stops services (watchdog self-heal /
    a direct fleet update on a runner); None means the whole cluster
    (the rollout's stop-the-world).

    Args:
        source: tags the signal's origin (e.g. 'system:update').
        exclude_agent_ids: agents to skip in the bulk insert. The quiesce
            convergence loop passes its already-signalled set, so each pass
            signals only agents that newly became live — an agent respawned
            mid-quiesce, or one whose spawn completed mid-quiesce.
        machine: restrict to agents running on this machine (None = all).
    """
    with db.write_transaction() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO inbound_messages (agent_id, content, kind, source) "  # noqa: S608 — ALIVE_SQL is a module constant
            "SELECT id, '', 'restart', %s FROM agents_meta "
            f"WHERE {ALIVE_SQL} AND NOT (id = ANY(%s)) "
            "AND (%s::text IS NULL OR machine = %s) "
            "RETURNING agent_id",
            (source, list(ALIVE_STATUSES), list(exclude_agent_ids), machine, machine),
        )
        ids = [row[0] for row in cur.fetchall()]
        conn.commit()
    # Publish a wake per signalled agent (see insert_inbound_message + the
    # publish_inbound_wake docstring). Best-effort: a lost publish is recovered
    # by the agent's SELECT recheck. restart carries no user-facing inbound id,
    # so "0" (mirroring insert_compact_request_inbound).
    for aid in ids:
        publish_inbound_wake(db, bus, aid, "0")
    return ids


def list_live_agent_ids(db: Database, machine: str | None = None) -> list[int]:
    """IDs of agents currently holding a process (status running/idling).

    `machine` restricts to one host's agents (the per-host quiesce); None
    returns the whole cluster (the rollout's stop-the-world).
    """
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM agents_meta "  # noqa: S608 — ALIVE_SQL is a module constant
            f"WHERE {ALIVE_SQL} AND (%s::text IS NULL OR machine = %s)",
            (list(ALIVE_STATUSES), machine, machine),
        )
        return [row[0] for row in cur.fetchall()]
