"""Postgres operations shared by UI and kernel.

`shared/` is the UI/kernel boundary layer — `ui/*.py` does not import
`agent.*`; the two sides couple only via DB + Redis. This package
centralizes helpers used by both ends (`config` / `db` / `events` /
`exit_codes` / `schema.sql`).

This module contains **pure SQL, no business semantics, used by both
ends** helpers. Kernel-only (inbound claim, wait/mark/revert) is in
`agent/db.py`. Connection policy and pool construction live in
`shared/db_connections.py` and remain re-exported here.
"""

import contextlib
import json
from collections.abc import Collection
from datetime import UTC, datetime
from typing import Any, NamedTuple

import psycopg

from shared.agents import AgentStatus
from shared.db_connections import DEFAULT_POOL_TIMEOUT_S as DEFAULT_POOL_TIMEOUT_S
from shared.db_connections import PG_KEEPALIVE_KWARGS as PG_KEEPALIVE_KWARGS
from shared.db_connections import (
    PG_POOLED_BASELINE_RESTORE_SQL as PG_POOLED_BASELINE_RESTORE_SQL,
)
from shared.db_connections import (
    PG_STATEMENT_TIMEOUT_KWARGS as PG_STATEMENT_TIMEOUT_KWARGS,
)
from shared.db_connections import (
    PG_STATEMENT_TIMEOUT_OPTIONS as PG_STATEMENT_TIMEOUT_OPTIONS,
)
from shared.db_connections import (
    PG_STATEMENT_TIMEOUT_SET_SQL as PG_STATEMENT_TIMEOUT_SET_SQL,
)
from shared.db_connections import UNANCHORED_DB_SENTINEL as UNANCHORED_DB_SENTINEL
from shared.db_connections import UnanchoredHomeError as UnanchoredHomeError
from shared.db_connections import _guard_db_url as _guard_db_url
from shared.db_connections import _restore_pooled_session as _restore_pooled_session
from shared.db_connections import (
    _restore_pooled_session_async as _restore_pooled_session_async,
)
from shared.db_connections import connect as connect
from shared.db_connections import direct_db_url as direct_db_url
from shared.db_connections import pool as pool
from shared.db_transaction import write_transaction
from shared.inbound_provenance import InboundProvenance, content_sha256, source_assertion_match
from shared.log import logger


class InboundRow(NamedTuple):
    """One row of inbound_messages (for timeline reads)."""

    id: int
    content: str
    kind: str
    source: str | None
    status: str
    created_at: datetime
    claimed_at: datetime | None = None


def fetch_one(cur: psycopg.Cursor, context: str) -> tuple[Any, ...]:
    """After `fetchone()`, assert there was a row — for
    `INSERT ... RETURNING` / aggregate queries where SQL contractually
    guarantees "exactly one row". `python -O` swallows assert, so we
    explicitly raise.

    `context` is a short tag from the caller (e.g. `"insert agent-1"`)
    — call sites are thin wrappers; the traceback pointing at the
    helper alone cannot tell which query failed; reading `cur.query`
    in the helper layer relies on unstable private API. In tests
    `assert` is fine; `python -O` does not run tests."""
    row = cur.fetchone()
    if row is None:
        raise RuntimeError(f"expected exactly one row: {context}")
    return row


def create_agent(db: psycopg.Connection) -> int:
    """Create a new agent (agents + agents_meta row). label left NULL
    ("not set" semantics) — frontend fallback `#N`.

    The `agents` table name is constrained by LangGraph wire
    (`config["configurable"]["thread_id"]`); the public API of this
    function is "create agent". Also INSERTs the agents_meta row —
    per-agent counters for shell/monitor/schedule depend on the
    agents_meta row existing. The spawn path (gateway POST
    /api/agents with prompt) uses a BackgroundTask to LLM-generate a
    short name in the background and CAS-write; non-spawn paths
    (this function, eval callers) do not auto-name; the caller is
    expected to PATCH /api/agents/{id} manually as needed.
    """
    with db.cursor() as cur:
        cur.execute("INSERT INTO agents DEFAULT VALUES RETURNING id")
        new_id = fetch_one(cur, "insert new agent")[0]
    db.commit()
    return new_id


def agent_exists(db: psycopg.Connection, agent_id: int) -> bool:
    """Check whether an agent exists. Web endpoints use this as a 404 precondition."""
    with db.cursor() as cur:
        cur.execute("SELECT 1 FROM agents WHERE id = %s", (agent_id,))
        return cur.fetchone() is not None


def list_agents(db: psycopg.Connection) -> list[tuple[int, str | None]]:
    """Return all agents: (id, label). label None means "not set" —
    frontend fallback shows `#N` (see db/schema.sql `agents.label` comment)."""
    with db.cursor() as cur:
        cur.execute("SELECT id, label FROM agents ORDER BY id ASC")
        return cur.fetchall()


def publish_inbound_wake(agent_id: int, payload: str) -> bool:
    """Best-effort Redis publish to wake an idle agent — the fast path paired
    with the claim loop's SELECT recheck. Also SETEXes the agent's wake key
    (`shared.cluster.wake_key`) as a durable breadcrumb so a wake lost to a
    disconnected listener is recovered on the listener's next (re)subscribe
    instead of waiting out the full recheck budget.

    Never raises: a wake lost here is recovered by `wait_for_inbound`'s SELECT
    within `timeout_s`, so the caller's INSERT+commit is never held hostage to
    Redis. Returns True when the wake reached Redis, False when the publish
    was rejected or skipped — callers that meter delivery (the delivery
    watchdog's dispatch counter) must read the return value, never assume
    success. Ignoring the return value stays backward compatible.
    But the failure is NOT swallowed blindly — a `NoPermissionError`
    (a `ResponseError`) means the publisher's redis ACL user is not granted this
    cluster's `<prefix>:inbound:*` channel (a channel-prefix or ACL misconfig),
    which would silently disable instant wake fleet-wide, so it is logged at
    WARNING. Transient failures (redis down) log at DEBUG. Channel is derived via
    `inbound_channel` so publish and `RedisInboundListener` subscribe stay in
    sync and stay inside the ACL grant."""
    from redis.exceptions import ResponseError

    from shared.cluster import WAKE_KEY_TTL_S, inbound_channel, wake_key
    from shared.redis_client import sync_redis

    channel = inbound_channel(agent_id)
    try:
        r = sync_redis()
        try:
            # redis-py types publish()'s **kwargs as Unknown, so the bound
            # method reads as partially-unknown; the call itself is fully typed.
            r.publish(channel, payload)  # pyright: ignore[reportUnknownMemberType]
            # Durable breadcrumb for the lost-wake window: pub/sub is
            # fire-and-forget, so a publish that lands while the agent's
            # listener is disconnected is otherwise unrecoverable until the
            # claim loop's 30s SELECT recheck. The listener GETDELs this key
            # on (re)subscribe and SELECTs immediately when it is present.
            r.set(wake_key(agent_id), payload, ex=WAKE_KEY_TTL_S)
            return True
        finally:
            r.close()
    except ResponseError as exc:
        logger.warning(
            "inbound wake publish to {ch!r} rejected by redis ({exc!r}) — the "
            "cluster redis ACL user lacks this channel; instant wake off, agents "
            "fall back to their SELECT recheck. Check ensure_cluster_redis_acl.",
            ch=channel,
            exc=exc,
        )
        return False
    except Exception as exc:
        logger.debug(
            "inbound wake publish to {ch!r} skipped ({exc!r}) — best-effort; the "
            "agent's SELECT recheck delivers within timeout_s.",
            ch=channel,
            exc=exc,
        )
        return False


def insert_inbound_message(
    db: psycopg.Connection,
    agent_id: int,
    content: str,
    source: str,
    kind: str = "chat",
    payload: dict[str, object] | None = None,
    provenance: InboundProvenance | None = None,
) -> int:
    """UI / gateway call: INSERT one inbound; the agent's claim node
    fetches and dispatches.

    Args:
        source: provenance tag — claim node reads it and goes
            through `shared/envelope.py:wrap_inbound` envelope prefix to tell
            the agent who the message came from. The UI passes
            `'user'`; a peer agent passes `'agent:N'`.
        kind: default `'chat'` (user dialogue). Other valid values
            see `db/schema.sql` CHECK constraint: `'compact_summary'`
            / `'compact_request'` / `'cancel'` / `'terminate'` / `'restart'`
            / `'restart_completed'` / `'resurrect'`. Non-'chat' usually
            pairs with `content=''` (control signal without payload).
        payload: optional JSONB sidecar. For a multimodal chat inbound this
            carries `{"content_blocks": [...]}` (the OpenAI-shaped text/image
            blocks); the claim node reads it to build a native multimodal
            HumanMessage while `content` holds the text part for legacy /
            envelope / timeline readers. None leaves the column NULL.

    Returns:
        The newly inserted inbound id — the caller can use it to
        publish an `inbound_arrived` event for the web UI to show in
        real time (spec §5).
    """
    if payload is not None and "lifecycle_result" in payload:
        raise ValueError("lifecycle_result is reserved for verified command settlement")
    if payload is not None and "launch_attempts" in payload:
        raise ValueError("launch_attempts is reserved for controller authorization")
    if payload is not None and "target_process_identity" in payload:
        raise ValueError("target_process_identity is reserved for admitted lifecycle application")
    if payload is not None and "resurrection_retry" in payload:
        raise ValueError("resurrection_retry is reserved for the pending resurrection owner")
    if payload is not None and (
        {"resurrection_launch", "resurrection_launch_attempts"} & payload.keys()
    ):
        raise ValueError("resurrection launch evidence is reserved for the lifecycle owner")
    from shared.caller_identity import caller_payload
    from shared.envelope import reject_unnegotiated_caller

    reject_unnegotiated_caller(source)
    payload = caller_payload(source, payload)
    source_verified_by = provenance.source_verified_by if provenance is not None else None
    source_transport = provenance.source_transport if provenance is not None else None
    content_hash = content_sha256(content) if provenance is not None else None
    assertion_match = source_assertion_match(source, provenance) if provenance is not None else None
    # Map inbound kind → lifecycle event_type. Only chat messages between
    # agents produce a 'send_message' event; user→agent chat is not an
    # inter-agent event. Lifecycle kinds map 1:1 except compact_summary /
    # compact_request which are handled elsewhere (agent self-insert /
    # insert_compact_request_inbound).
    _kind_to_event: dict[str, str | None] = {
        "chat": "send_message",
        "system_note": "send_message",
        "terminate": "terminate",
        "restart": "restart",
        "cancel": "cancel",
        "resurrect": "resurrect",
        "restart_completed": "restart_completed",
        "fork": "fork",
    }
    event_type = _kind_to_event.get(kind)
    # Parse the lineage parent from source. For inter-agent chat it is the
    # sender; for a kind='fork' lifecycle inbound the source is the fork
    # identity marker "agent:{fork_source}" — and per the fork-lineage ruling
    # (2026-08-28, task #1879) the fork event's target_agent_id must be the
    # fork SOURCE (the lineage parent), never the executor.
    target_agent_id: int | None = None
    if event_type == "send_message" and source.startswith("agent:"):
        with contextlib.suppress(ValueError):
            target_agent_id = int(source.removeprefix("agent:"))
    elif event_type == "send_message":
        # user/UI → agent chat is not an inter-agent event; skip event write
        event_type = None
    elif event_type == "fork" and source.startswith("agent:"):
        with contextlib.suppress(ValueError):
            target_agent_id = int(source.removeprefix("agent:"))

    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO inbound_messages "
            "(agent_id, content, kind, source, payload, source_verified_by, "
            "source_transport, content_hash, source_assertion_match) "
            "VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s) RETURNING id",
            (
                agent_id,
                content,
                kind,
                source,
                json.dumps(payload) if payload else None,
                source_verified_by,
                source_transport,
                content_hash,
                assertion_match,
            ),
        )
        new_id = fetch_one(cur, "insert inbound message")[0]
        if event_type is not None:
            from shared.audit_events import insert_event_log

            insert_event_log(
                event_type=event_type,
                agent_id=agent_id,
                source=source,
                target_agent_id=target_agent_id,
                payload={"inbound_id": new_id, "content": content}
                if content
                else {"inbound_id": new_id},
            )
    db.commit()
    # Publish to Redis to wake the idle agent. Agents subscribe to
    # `<prefix>:inbound:{agent_id}` (inbound_channel) via RedisInboundListener.
    # Fire-and-forget: the agent's defensive SELECT recheck catches inbound
    # within timeout_s regardless — but a NOPERM is logged, not swallowed.
    publish_inbound_wake(agent_id, str(new_id))
    return new_id


def insert_restart_completed_inbound(
    cur: psycopg.Cursor,
    agent_id: int,
) -> tuple[str, str, dict[str, object] | None] | None:
    """Trace the newest restart inbound into a restart-completed marker.

    The newest row matters: an older ``system:update`` restart must not shadow
    a newer user or self restart, or the claim node renders the wrong marker
    wording. The payload passes through unchanged so the lifecycle marker can
    render this restart's config diff. After claiming the marker, the new
    process writes its full effective-config snapshot; this row guarantees the
    original restart envelope survives until then. ``None`` means no restart
    inbound exists; the caller owns the appropriate integrity or best-effort
    response.
    """
    cur.execute(
        "SELECT source, content, payload FROM inbound_messages "
        "WHERE agent_id = %s AND kind = 'restart' "
        "ORDER BY id DESC "
        "LIMIT 1",
        (agent_id,),
    )
    row = cur.fetchone()
    if row is None:
        return None
    source: str = row[0]
    content: str = row[1]
    payload: dict[str, object] | None = row[2]
    cur.execute("SELECT config_overlay FROM agents_meta WHERE id = %s", (agent_id,))
    config_overlay_row = fetch_one(cur, "restart-completed: read per-agent config")
    config_overlay: dict[str, object] | None = config_overlay_row[0]
    cur.execute(
        "INSERT INTO inbound_messages (agent_id, content, kind, source, payload) "
        "VALUES (%s, %s, 'restart_completed', %s, %s::jsonb)",
        (agent_id, content, source, json.dumps(payload) if payload else None),
    )
    from shared.audit_events import insert_event_log

    insert_event_log(
        event_type="restart_completed",
        agent_id=agent_id,
        source=source,
        payload={"config_overlay": config_overlay} if config_overlay else {},
    )
    return source, content, payload


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


# FYI notice (require_response=false) lifetime in the open queue: after this many
# days an unread FYI is auto-resolved ('read') — the open feed, the unread badge
# and the IM bridge stop carrying it (audit 05-gateway-lifecycle C1: open FYIs
# used to pile up forever). require_response notices NEVER expire — the user's
# answer is the only close. The expiry is applied as a query-side rule
# (created_at cutoff, `make_interval(days => ...)`) plus a lazy auto-resolve in
# the gateway's open-feed query, so no background sweeper is needed. One knob:
# change it here (and re-run the notice tests) to configure the TTL.
NOTICE_FYI_TTL_DAYS = 30


def signal_live_agents_restart(
    source: str, *, exclude_agent_ids: Collection[int] = (), machine: str | None = None
) -> list[int]:
    """Bulk-INSERT one kind='restart' inbound per live agent, wake each over Redis; return ids.

    The set-based form of the per-agent restart path (gateway
    restart_agent_op -> insert_inbound_message(kind='restart')): same
    inbound_messages contract, one INSERT ... SELECT over every live agent, and
    the same per-agent Redis wake so an *idling* agent restarts now instead of
    stalling to its SELECT recheck. That prompt drain matters here specifically:
    this is `ava cluster update`'s quiesce step, whose convergence loop keeps signalling
    until every live agent has drained — a 30s recheck lag per idle agent would
    drag the whole quiesce out. `source` tags the signal's origin
    (e.g. 'system:update').

    `machine` scopes the signal to one host's agents — the per-host quiesce the
    agent-runner self-update runs before it stops services (watchdog self-heal /
    a direct `ava cluster update` on a runner); None means the whole cluster
    (the rollout's stop-the-world).

    Args:
        source: tags the signal's origin (e.g. 'system:update').
        exclude_agent_ids: agents to skip in the bulk insert. The quiesce
            convergence loop passes its already-signalled set, so each pass
            signals only agents that newly became live — an agent respawned
            mid-quiesce, or one whose spawn completed mid-quiesce.
        machine: restrict to agents running on this machine (None = all).
    """
    with write_transaction() as conn, conn.cursor() as cur:
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
        publish_inbound_wake(aid, "0")
    return ids


def list_live_agent_ids(machine: str | None = None) -> list[int]:
    """IDs of agents currently holding a process (status running/idling).

    `machine` restricts to one host's agents (the per-host quiesce); None
    returns the whole cluster (the rollout's stop-the-world).
    """
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM agents_meta "  # noqa: S608 — ALIVE_SQL is a module constant
            f"WHERE {ALIVE_SQL} AND (%s::text IS NULL OR machine = %s)",
            (list(ALIVE_STATUSES), machine, machine),
        )
        return [row[0] for row in cur.fetchall()]


def list_pending_inbounds(db: psycopg.Connection, agent_id: int) -> list[InboundRow]:
    """List chat inbounds still queued for an agent, oldest first.

    Only `status='pending'` (the claim node has not picked these up yet)
    and `kind='chat'` (user-visible dialogue, not control signals). Once a
    message is claimed it enters the agent's in-memory messages and shows
    up in the timeline snapshot, so it is intentionally excluded here — the
    web UI renders these as a compact "pending" strip above the composer,
    distinct from the timeline.
    """
    with db.cursor() as cur:
        cur.execute(
            "SELECT id, content, kind, source, status, created_at, claimed_at "
            "FROM inbound_messages "
            "WHERE agent_id = %s AND status = 'pending' AND kind = 'chat' "
            "ORDER BY created_at ASC",
            (agent_id,),
        )
        return [InboundRow(*row) for row in cur.fetchall()]


def insert_compact_request_inbound(db: psycopg.Connection, agent_id: int) -> int:
    """UI / admin call: insert one kind='compact_request' inbound —
    the claim Node, on receiving, runs the backend Compaction LLM to
    generate a summary that replaces messages.

    The new design (Step 2 cleanup) merges the old
    framework_compact / agent_compact kinds: the user view no longer
    distinguishes modes; backend LLM summary generation is the unified
    path. Agent-initiated compact still goes through
    ava.self.compact() -> kind='compact_summary' (agent writes its
    own summary)."""
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO inbound_messages (agent_id, content, kind) VALUES (%s, %s, %s) "
            "RETURNING id",
            (agent_id, "", "compact_request"),
        )
        row = cur.fetchone()
        if row is None:
            raise RuntimeError("compact request inbound INSERT returned no id")
        inbound_id = row[0]
        from shared.audit_events import insert_event_log

        insert_event_log(
            event_type="compact",
            agent_id=agent_id,
            source="user",
            payload={"compact_kind": "request"},
        )
    db.commit()
    # Publish to Redis for agent wake-up (see insert_inbound_message + the
    # publish_inbound_wake docstring).
    publish_inbound_wake(agent_id, str(inbound_id))
    return inbound_id


def list_inbound_messages(
    db: psycopg.Connection,
    agent_id: int,
    limit: int = 500,
) -> list[InboundRow]:
    """Read inbound_messages rows for the given agent (including
    done) in created_at ascending order.

    Used by the /timeline endpoint — fetched when merging three
    sources for external inbound records.
    """
    with db.cursor() as cur:
        cur.execute(
            "SELECT id, content, kind, source, status, created_at, claimed_at "
            "FROM inbound_messages "
            "WHERE agent_id = %s ORDER BY created_at ASC LIMIT %s",
            (agent_id, limit),
        )
        return [InboundRow(*r) for r in cur.fetchall()]
