"""Route failed CI, QA, and merge work back through the agent lineage."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, NamedTuple, cast

from fastapi import APIRouter, HTTPException, Request
from psycopg_pool import ConnectionPool

from gateway.routers._delivery import deliver_chat_inbound
from gateway.routers._webhook_auth import authenticate_webhook
from gateway.schemas.work_failed import FailureDeliveryKind, WorkFailedIn, WorkFailedResult
from shared.agents import AgentStatus
from shared.config import settings
from shared.db import ALIVE_STATUSES, fetch_one
from shared.db_transaction import write_transaction
from shared.inbound_provenance import InboundProvenance

router = APIRouter()
_log = logging.getLogger(__name__)

_MAX_SPAWNER_ANCESTOR_DEPTH = 32
_MAX_REDELIVERY_ATTEMPTS = 3
_RECONCILE_BATCH = 100


class _StoredFailure(NamedTuple):
    event_id: int
    inserted: bool
    delivered_to: str | None
    delivery_kind: FailureDeliveryKind | None


class _RetryFailure(NamedTuple):
    event_id: int
    body: WorkFailedIn
    delivery_attempts: int


def _register_failure(pool: ConnectionPool[Any], body: WorkFailedIn) -> _StoredFailure:
    """Claim a dedup key before any delivery effect."""
    with write_transaction(pool) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO work_failed_events "
            "(repo, ref, commit_sha, stage, summary, author_agent_id, dedup_key) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (dedup_key) DO NOTHING "
            "RETURNING id, delivered_to, delivery_kind",
            (
                body.repo,
                body.ref,
                body.commit_sha,
                body.stage,
                body.summary,
                body.author_agent_id,
                body.dedup_key,
            ),
        )
        row = cur.fetchone()
        if row is not None:
            return _StoredFailure(
                event_id=int(row[0]),
                inserted=True,
                delivered_to=row[1],
                delivery_kind=cast(FailureDeliveryKind | None, row[2]),
            )
        cur.execute(
            "SELECT id, delivered_to, delivery_kind FROM work_failed_events WHERE dedup_key = %s",
            (body.dedup_key,),
        )
        existing = fetch_one(cur, "select duplicate work failure")
        return _StoredFailure(
            event_id=int(existing[0]),
            inserted=False,
            delivered_to=existing[1],
            delivery_kind=cast(FailureDeliveryKind | None, existing[2]),
        )


def _claim_stale_failures(
    pool: ConnectionPool[Any],
    *,
    grace_seconds: float,
) -> list[_RetryFailure]:
    """Claim one bounded stale batch and count each redelivery attempt."""
    with write_transaction(pool) as conn, conn.cursor() as cur:
        cur.execute(
            "WITH candidates AS ("
            "  SELECT id FROM work_failed_events "
            "  WHERE delivered_at IS NULL "
            "    AND created_at < now() - make_interval(secs => %s) "
            "  ORDER BY created_at, id LIMIT %s FOR UPDATE SKIP LOCKED"
            ") "
            "UPDATE work_failed_events AS event "
            "SET delivery_attempts = event.delivery_attempts + 1 "
            "FROM candidates WHERE event.id = candidates.id "
            "  AND event.delivered_at IS NULL "
            "RETURNING event.id, event.repo, event.ref, event.commit_sha, event.stage, "
            "event.summary, event.author_agent_id, event.dedup_key, event.delivery_attempts",
            (grace_seconds, _RECONCILE_BATCH),
        )
        return [
            _RetryFailure(
                event_id=int(row[0]),
                body=WorkFailedIn(
                    repo=row[1],
                    ref=row[2],
                    commit_sha=row[3],
                    stage=row[4],
                    summary=row[5],
                    author_agent_id=row[6],
                    dedup_key=row[7],
                ),
                delivery_attempts=int(row[8]),
            )
            for row in cur.fetchall()
        ]


def _agent_state(pool: ConnectionPool[Any], agent_id: int) -> tuple[AgentStatus, bool]:
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT status, status = ANY(%s) AND lease_expires_at > now() "
            "FROM agents_meta WHERE id = %s",
            (list(ALIVE_STATUSES), agent_id),
        )
        row = cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"agent {agent_id} not found")
    return AgentStatus(row[0]), bool(row[1])


def _alive_ancestors(pool: ConnectionPool[Any], author_agent_id: int) -> list[int]:
    """Return immutable birth ancestors nearest-first under canonical liveness."""
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "WITH RECURSIVE spawner_walk(agent_id, spawner, status, lease_expires_at, path, depth) "
            "AS ("
            "  SELECT id, COALESCE(born_spawner, spawner), status, lease_expires_at, ARRAY[id], 0 "
            "  FROM agents_meta WHERE id = %s "
            "  UNION ALL "
            "  SELECT parent.id, COALESCE(parent.born_spawner, parent.spawner), "
            "         parent.status, parent.lease_expires_at, walk.path || parent.id, walk.depth + 1 "
            "  FROM spawner_walk walk "
            "  JOIN agents_meta parent "
            "    ON parent.id = substring(walk.spawner FROM '^agent:([0-9]+)$')::bigint "
            "  WHERE walk.depth < %s AND NOT parent.id = ANY(walk.path)"
            ") "
            "SELECT agent_id FROM spawner_walk "
            "WHERE depth > 0 AND status = ANY(%s) AND lease_expires_at > now() "
            "ORDER BY depth",
            (author_agent_id, _MAX_SPAWNER_ANCESTOR_DEPTH, list(ALIVE_STATUSES)),
        )
        return [cast(int, row[0]) for row in cur.fetchall()]


def _failure_message(body: WorkFailedIn) -> str:
    return (
        f"Work failed for author agent {body.author_agent_id}: repo={body.repo} "
        f"ref={body.ref} commit={body.commit_sha} stage={body.stage}\n\n{body.summary}"
    )


def _finish_delivery(
    pool: ConnectionPool[Any],
    event_id: int,
    *,
    delivered_to: str,
    delivery_kind: FailureDeliveryKind,
) -> bool:
    with write_transaction(pool) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE work_failed_events SET delivered_to = %s, delivery_kind = %s, "
            "delivered_at = now() WHERE id = %s AND delivered_at IS NULL",
            (delivered_to, delivery_kind, event_id),
        )
        return cur.rowcount == 1


def _duplicate_delivery_result(pool: ConnectionPool[Any], event_id: int) -> WorkFailedResult:
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT delivered_to, delivery_kind, delivered_at "
            "FROM work_failed_events WHERE id = %s",
            (event_id,),
        )
        row = fetch_one(cur, "select completed work failure")
    if row[2] is None:
        raise RuntimeError(f"work failure {event_id} lost its delivery claim")
    return WorkFailedResult(
        event_id=event_id,
        status="duplicate",
        delivered_to=row[0],
        delivery_kind=cast(FailureDeliveryKind, row[1]),
    )


def _create_task_alert(
    pool: ConnectionPool[Any], event_id: int, body: WorkFailedIn
) -> WorkFailedResult:
    """Persist the no-live-lineage outcome in the existing task registry."""
    description = f"repo={body.repo} commit={body.commit_sha} stage={body.stage}\n\n{body.summary}"
    with write_transaction(pool) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT delivered_to, delivery_kind, delivered_at "
            "FROM work_failed_events WHERE id = %s FOR UPDATE",
            (event_id,),
        )
        existing = fetch_one(cur, "lock work failure for task alert")
        if existing[2] is not None:
            return WorkFailedResult(
                event_id=event_id,
                status="duplicate",
                delivered_to=existing[0],
                delivery_kind=cast(FailureDeliveryKind, existing[1]),
            )
        cur.execute("SELECT id FROM agent_tasks WHERE is_root ORDER BY id LIMIT 1")
        root_id = int(fetch_one(cur, "select task registry root")[0])
        cur.execute(
            "INSERT INTO agent_tasks "
            "(parent_id, title, description, created_by, owner, priority) "
            "VALUES (%s, %s, %s, 'system', %s, 'P1') RETURNING id",
            (
                root_id,
                f"[work-failed:{event_id}] {body.stage} {body.repo}@{body.commit_sha[:12]}",
                description,
                body.author_agent_id,
            ),
        )
        task_id = int(fetch_one(cur, "insert work failure task alert")[0])
        delivered_to = f"task:{task_id}"
        cur.execute(
            "UPDATE work_failed_events SET delivered_to = %s, delivery_kind = 'task_alert', "
            "delivered_at = now() WHERE id = %s AND delivered_at IS NULL",
            (delivered_to, event_id),
        )
        if cur.rowcount != 1:
            raise RuntimeError(f"work failure {event_id} lost its locked delivery claim")
    return WorkFailedResult(
        event_id=event_id,
        status="task_alerted",
        delivered_to=delivered_to,
        delivery_kind="task_alert",
    )


async def _deliver_failure(
    pool: ConnectionPool[Any],
    event_id: int,
    body: WorkFailedIn,
    provenance: InboundProvenance,
) -> WorkFailedResult:
    initial_status, _ = await asyncio.to_thread(_agent_state, pool, body.author_agent_id)
    message = _failure_message(body)
    await deliver_chat_inbound(
        pool,
        body.author_agent_id,
        prepare=lambda _conn: message,
        source="system",
        client_message_id=f"work-failed:{event_id}:agent:{body.author_agent_id}",
        provenance=provenance,
    )
    _, author_alive = await asyncio.to_thread(_agent_state, pool, body.author_agent_id)
    if author_alive:
        delivery_kind: FailureDeliveryKind = (
            "author_resurrected" if initial_status is AgentStatus.TERMINATED else "author"
        )
        delivered_to = f"agent:{body.author_agent_id}"
        completed = await asyncio.to_thread(
            _finish_delivery,
            pool,
            event_id,
            delivered_to=delivered_to,
            delivery_kind=delivery_kind,
        )
        if not completed:
            return await asyncio.to_thread(_duplicate_delivery_result, pool, event_id)
        return WorkFailedResult(
            event_id=event_id,
            status="delivered",
            delivered_to=delivered_to,
            delivery_kind=delivery_kind,
        )

    ancestors = await asyncio.to_thread(_alive_ancestors, pool, body.author_agent_id)
    for ancestor_id in ancestors:
        await deliver_chat_inbound(
            pool,
            ancestor_id,
            prepare=lambda _conn: message,
            source="system",
            client_message_id=f"work-failed:{event_id}:agent:{ancestor_id}",
            provenance=provenance,
        )
        _, ancestor_alive = await asyncio.to_thread(_agent_state, pool, ancestor_id)
        if not ancestor_alive:
            continue
        delivered_to = f"agent:{ancestor_id}"
        completed = await asyncio.to_thread(
            _finish_delivery,
            pool,
            event_id,
            delivered_to=delivered_to,
            delivery_kind="delegator",
        )
        if not completed:
            return await asyncio.to_thread(_duplicate_delivery_result, pool, event_id)
        return WorkFailedResult(
            event_id=event_id,
            status="delivered",
            delivered_to=delivered_to,
            delivery_kind="delegator",
        )

    return await asyncio.to_thread(_create_task_alert, pool, event_id, body)


async def reconcile_stale_work_failures(pool: ConnectionPool[Any]) -> int:
    """Retry stale unfinished deliveries; isolate one bad event from the batch."""
    failures = await asyncio.to_thread(
        _claim_stale_failures,
        pool,
        grace_seconds=settings.gateway.work_failed_retry_grace_seconds,
    )
    completed = 0
    for failure in failures:
        try:
            if failure.delivery_attempts > _MAX_REDELIVERY_ATTEMPTS:
                result = await asyncio.to_thread(
                    _create_task_alert, pool, failure.event_id, failure.body
                )
            else:
                result = await _deliver_failure(
                    pool,
                    failure.event_id,
                    failure.body,
                    InboundProvenance(
                        source_verified_by=None,
                        source_transport="reconcile",
                    ),
                )
            if result.status != "duplicate":
                completed += 1
        except Exception:
            _log.warning(
                "[work-failed-reconcile] delivery failed for event %s on attempt %s",
                failure.event_id,
                failure.delivery_attempts,
                exc_info=True,
            )
    return completed


@router.post("/api/work-failed")
async def post_work_failed(body: WorkFailedIn, request: Request) -> WorkFailedResult:
    """Record one failure and route it once without enforcing source assertions."""
    authentication = authenticate_webhook(request, provider="work_failed")
    if not authentication.authorized:
        raise HTTPException(status_code=401, detail="unauthorized webhook caller")
    await asyncio.to_thread(_agent_state, request.app.state.db_pool, body.author_agent_id)
    stored = await asyncio.to_thread(_register_failure, request.app.state.db_pool, body)
    if not stored.inserted:
        return WorkFailedResult(
            event_id=stored.event_id,
            status="duplicate",
            delivered_to=stored.delivered_to,
            delivery_kind=stored.delivery_kind,
        )
    return await _deliver_failure(
        request.app.state.db_pool,
        stored.event_id,
        body,
        InboundProvenance(
            source_verified_by=authentication.source_verified_by,
            source_transport="http",
        ),
    )
