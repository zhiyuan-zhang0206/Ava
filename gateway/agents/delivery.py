"""Shared chat-inbound delivery for the gateway routers.

REST/SDK chat, MCP send_message, file uploads and completion digests
all do the same thing: persist one 'chat' inbound for an agent and announce it
for the live UI. `deliver_chat_inbound` is that one path, so each route keeps
only its own precondition (the `prepare` callback) instead of re-inlining the
INSERT + publish dance.
"""

from __future__ import annotations

import asyncio
import functools
from collections.abc import Callable
from typing import NamedTuple

import psycopg
from psycopg_pool import ConnectionPool

from base.agents import AgentStatus
from base.agents.messages.chat_delivery import (
    ChatInboundCommittedError,
    ChatInboundReceipt,
    insert_chat_inbound_once,
    reconcile_chat_inbound,
)
from base.agents.messages.inbound import InboundKind
from base.agents.messages.inbound_provenance import InboundProvenance
from base.db import Database, publish_inbound_wake
from base.events.live.announce import publish_agent_updated_sync
from base.events.live.bus import EventBus
from ops import lifecycle as _ops
from ops.agents import get_agent_status


class ChatDelivery(NamedTuple):
    """Durable inbound receipt plus the status observed after auto-resurrect."""

    status: AgentStatus
    inbound_id: int | None


async def deliver_chat_inbound(
    pool: ConnectionPool,
    db: Database,
    bus: EventBus,
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

    Live publication completes in this caller. Known transport failures retain
    the bus's bounded best-effort policy; other post-commit failures propagate
    with the durable receipt. A failed notification never undoes the INSERT.
    """
    inbound: tuple[ChatInboundReceipt, str] | None = await asyncio.to_thread(
        _deliver_blocking,
        pool,
        db,
        bus,
        agent_id,
        prepare,
        source,
        payload,
        client_message_id,
        provenance,
    )
    if inbound is None:
        if refresh_badge:
            await asyncio.to_thread(publish_agent_updated_sync, bus, agent_id)
        status = await asyncio.to_thread(get_agent_status, db, agent_id)
        return ChatDelivery(status, None)
    receipt, content = inbound
    return await _finish_chat_delivery(
        db,
        bus,
        agent_id,
        receipt,
        content,
        source,
        client_message_id,
        refresh_badge=refresh_badge,
    )


async def _finish_chat_delivery(
    db: Database,
    bus: EventBus,
    agent_id: int,
    receipt: ChatInboundReceipt,
    content: str,
    source: str,
    client_message_id: str | None,
    *,
    refresh_badge: bool = False,
) -> ChatDelivery:
    """Await separate wake, UI and resurrection effects after durable commit."""
    try:
        if refresh_badge:
            await asyncio.to_thread(publish_agent_updated_sync, bus, agent_id)
        if receipt.pending:
            if not receipt.inserted:
                # Same-key recovery heals a missed wake without another INSERT.
                await asyncio.to_thread(
                    publish_inbound_wake, db, bus, agent_id, str(receipt.inbound_id)
                )
            await _ops.publish_inbound_arrived(
                bus, agent_id, receipt.inbound_id, "chat", source, content
            )
            status = await _ops.resurrect_if_terminated(
                db,
                bus,
                agent_id,
                trigger_inbound_id=receipt.inbound_id,
                trigger_inbound_kind=InboundKind.CHAT,
            )
        else:
            # Claimed/done work is a receipt lookup, never a stale resurrection.
            status = await asyncio.to_thread(get_agent_status, db, agent_id)
    except Exception as exc:
        raise ChatInboundCommittedError(receipt, client_message_id, exc) from exc
    return ChatDelivery(status, receipt.inbound_id)


async def reconcile_chat_delivery(
    pool: ConnectionPool,
    db: Database,
    bus: EventBus,
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
    return await _finish_chat_delivery(
        db, bus, agent_id, receipt, content, source, client_message_id
    )


def _deliver_blocking(
    pool: ConnectionPool,
    db: Database,
    bus: EventBus,
    agent_id: int,
    prepare: Callable[[psycopg.Connection], str | None],
    source: str,
    payload: dict[str, object] | None,
    client_message_id: str | None,
    provenance: InboundProvenance | None,
) -> tuple[ChatInboundReceipt, str] | None:
    """Sync delivery transaction — via to_thread: `prepare` runs inside it (it
    may execute its own DB statements), then the inbound INSERT. Returns
    (receipt, content) or None when prepare returned None."""
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
                publish_wake=functools.partial(publish_inbound_wake, db, bus),
            )
            return (receipt, content)
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
