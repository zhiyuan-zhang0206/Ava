"""Shared chat-inbound delivery for the gateway routers.

Several endpoints (question / report replies, SDK send_message, file uploads)
all do the same thing: persist one 'chat' inbound for an agent and announce it
for the live UI. `deliver_chat_inbound` is that one path, so each route keeps
only its own precondition (the `prepare` callback) instead of re-inlining the
INSERT + publish dance.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from typing import Any, NamedTuple

import psycopg
from fastapi import HTTPException
from psycopg_pool import ConnectionPool

from ops import ops_lifecycle as _ops
from ops.agents import get_agent_status
from shared.agents import AgentStatus
from shared.caller_protocol import CallerProtocolUnavailableError
from shared.chat_delivery import (
    ChatInboundReceipt,
    insert_chat_inbound_once,
    reconcile_chat_inbound,
)
from shared.db import publish_inbound_wake
from shared.inbound_provenance import InboundProvenance
from shared.live_announce import publish_agent_updated_sync
from shared.log import logger

# Strong references for fire-and-forget publishes: asyncio's event loop holds
# only a WEAK reference to tasks, so an unreferenced task can be garbage
# collected mid-flight (audit cc-backend-runtime P2 — the previous noqa
# comment claimed the loop holds a strong one, which it does not). The
# done-callback drops each task once it finishes.
_background_tasks: set[asyncio.Task[object]] = set()


class ChatDelivery(NamedTuple):
    """Durable inbound receipt plus the status observed after auto-resurrect."""

    status: AgentStatus
    inbound_id: int | None


def _spawn_background(coro: Coroutine[Any, Any, object]) -> None:
    """Start `coro` as a fire-and-forget task kept alive until completion."""
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def deliver_chat_inbound(
    pool: ConnectionPool,
    agent_id: int,
    *,
    prepare: Callable[[psycopg.Connection], str | None],
    source: str = "user",
    refresh_badge: bool = False,
    payload: dict[str, object] | None = None,
    client_message_id: str | None = None,
    provenance: InboundProvenance | None = None,
) -> ChatDelivery:
    """Deliver one 'chat' inbound to `agent_id` and announce it for the live UI.

    `prepare` runs inside the delivery transaction and returns the inbound text
    (or None to deliver no message, e.g. a report dismissed without a reply). It
    is where a route enforces its own precondition — marking a question answered,
    a report read — and may raise to abort before anything is written. When it
    returns text, the same transaction INSERTs it as the inbound so the agent's
    claim picks it up; when `refresh_badge`, the agent row is refreshed too (for
    endpoints whose unread counts change). `payload` is the optional JSONB
    sidecar written alongside — a multimodal message passes
    `{"content_blocks": [...]}` here while `prepare` returns the text part.
    After commit, reads delivery-time status and publishes InboundArrived.
    `client_message_id`, when present, identifies the logical message at the
    inbound INSERT itself: a same-id retry returns the existing inbound instead
    of duplicating it. Returns the delivery-time status and durable inbound id
    for the AgentMessageEnqueued response.

    The Redis publish (InboundArrived for frontend SSE) is dispatched as a
    background task so a slow Redis never delays the HTTP response — the
    full content is carried in the event and a large message body used to
    add publish latency before this was made non-blocking.
    """
    try:
        inbound: tuple[int, str, bool, bool] | None = await asyncio.to_thread(
            _deliver_blocking,
            pool,
            agent_id,
            prepare,
            source,
            payload,
            client_message_id,
            provenance,
        )
    except CallerProtocolUnavailableError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    # The connection block commits on exit. Publish the badge refresh only AFTER
    # that commit, on a fresh connection: keeping it inside the block coupled a
    # redis outage to a rollback of the user's inbound INSERT (a publish raising
    # inside the `with` unwinds the transaction), and announced an AgentUpdated
    # snapshot before the write was durable. The whole step is off the delivery's
    # critical path — the inbound is already durable — so it degrades gracefully:
    # publish_agent_updated_sync's publish is never-raise, but its fresh-connection
    # snapshot READ could still fail; a failure there must not 500 the delivery or
    # skip the InboundArrived + resurrect below, so it is logged and swallowed.
    if refresh_badge:
        try:
            await asyncio.to_thread(_badge_refresh_blocking, pool, agent_id)
        except Exception as exc:
            logger.warning(
                "deliver_chat_inbound: badge refresh for agent {aid} failed after "
                "commit ({exc!r}); the inbound is delivered — degrading the live "
                "badge update, InboundArrived + resurrect continue.",
                aid=agent_id,
                exc=exc,
            )
    if inbound is None:
        # Nothing delivered (e.g. a report dismissed without a reply) — just
        # report the delivery-time status, no announce and no resurrect.
        # Off the event loop: get_agent_status opens a fresh DB connection.
        status = await asyncio.to_thread(get_agent_status, agent_id)
        return ChatDelivery(status, None)
    # Fire-and-forget: publish the InboundArrived event to Redis for the
    # frontend SSE channel.  The full content is in the event payload;
    # a large message body can make this publish take non-trivial time,
    # and there is no reason to hold the HTTP response for it — the DB
    # commit already guarantees the inbound is queued. The task is held in
    # a module-level set so the loop's weak reference cannot let it be
    # GC'd before the publish lands.
    inbound_id, content, inserted, pending = inbound
    if pending and not inserted:
        # A same-key retry is also the recovery path for a process death after
        # COMMIT but before the original wake. Redis wake is idempotent and the
        # exact pending-row guard below makes resurrection safe to repeat.
        await asyncio.to_thread(publish_inbound_wake, agent_id, str(inbound_id))
    if pending:
        _spawn_background(
            _ops.publish_inbound_arrived(agent_id, inbound_id, "chat", source, content)
        )
        # Auto-resurrect: a chat delivered to a terminated agent should wake it so
        # the sender's message gets a response — the user's reply (or any peer /
        # watcher message) implies they want the agent alive to handle it. Shared
        # with the compact path via `resurrect_if_terminated`.
        status = await _ops.resurrect_if_terminated(
            agent_id,
            trigger_inbound_id=inbound_id,
            trigger_inbound_kind="chat",
        )
    else:
        # The durable row was already claimed/done. Reconciliation is a receipt
        # lookup only in this state: never resurrect from stale work.
        status = await asyncio.to_thread(get_agent_status, agent_id)
    return ChatDelivery(status, inbound_id)


async def reconcile_chat_delivery(
    pool: ConnectionPool,
    agent_id: int,
    *,
    client_message_id: str,
    content: str,
    source: str,
    payload: dict[str, object] | None,
) -> ChatDelivery | None:
    """Resolve an uncertain send and heal any still-pending delivery tail.

    Receipt lookup is immutable and side-effect free. When the row is still
    pending, repeating the best-effort wake, live announcement, and exact-row
    resurrection closes the crash interval after COMMIT and before those
    effects. A claimed/done row returns its receipt without reviving stale work.
    """
    receipt = await asyncio.to_thread(
        _reconcile_blocking,
        pool,
        client_message_id,
        agent_id,
        content,
        source,
        payload,
    )
    if receipt is None:
        return None
    if not receipt.pending:
        status = await asyncio.to_thread(get_agent_status, agent_id)
        return ChatDelivery(status, receipt.inbound_id)
    await asyncio.to_thread(publish_inbound_wake, agent_id, str(receipt.inbound_id))
    _spawn_background(
        _ops.publish_inbound_arrived(agent_id, receipt.inbound_id, "chat", source, content)
    )
    status = await _ops.resurrect_if_terminated(
        agent_id,
        trigger_inbound_id=receipt.inbound_id,
        trigger_inbound_kind="chat",
    )
    return ChatDelivery(status, receipt.inbound_id)


def _deliver_blocking(
    pool: ConnectionPool,
    agent_id: int,
    prepare: Callable[[psycopg.Connection], str | None],
    source: str,
    payload: dict[str, object] | None,
    client_message_id: str | None,
    provenance: InboundProvenance | None,
) -> tuple[int, str, bool, bool] | None:
    """Sync delivery transaction — via to_thread: `prepare` runs inside it (it
    may execute its own DB statements), then the inbound INSERT. Returns
    (inbound_id, content, inserted, pending) or None when prepare returned None."""
    with pool.connection() as conn:
        content = prepare(conn)
        if content is not None:
            receipt = insert_chat_inbound_once(
                conn,
                agent_id=agent_id,
                content=content,
                source=source,
                payload=payload,
                client_message_id=client_message_id,
                provenance=provenance,
            )
            return (receipt.inbound_id, content, receipt.inserted, receipt.pending)
    return None


def _reconcile_blocking(
    pool: ConnectionPool,
    client_message_id: str,
    agent_id: int,
    content: str,
    source: str,
    payload: dict[str, object] | None,
) -> ChatInboundReceipt | None:
    with pool.connection() as conn:
        return reconcile_chat_inbound(
            conn,
            client_message_id=client_message_id,
            agent_id=agent_id,
            content=content,
            source=source,
            payload=payload,
        )


def _badge_refresh_blocking(pool: ConnectionPool, agent_id: int) -> None:
    """Sync unread-badge snapshot + publish — via to_thread (fresh connection;
    never-raise publish, but the snapshot read can fail)."""
    with pool.connection() as conn:
        publish_agent_updated_sync(conn, agent_id)
