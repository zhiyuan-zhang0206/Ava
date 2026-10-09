"""Versioned notice selectors requiring observed identity and transactional receipts."""

import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request

from gateway.agents.notice_operations.current import (
    GuardedNoticeEdit,
    ObservedNotice,
    mutate,
    operation_key,
)
from gateway.agents.notices import _publish_response_required_hint, post_notice_resolve
from gateway.agents.schemas import AgentMessageEnqueued, NoticeItem, ResolveNoticeIn
from ops import lifecycle

router = APIRouter()


@router.post(
    "/api/keyed/v1/agents/{agent_id}/notices/{notice_id}/resolve",
    status_code=201,
    dependencies=[Depends(operation_key)],
)
async def resolve_observed(
    agent_id: int,
    notice_id: int,
    body: ResolveNoticeIn,
    request: Request,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128)],
) -> AgentMessageEnqueued:
    """Resolve the explicit global row using verified transactional recovery."""
    # Admission validates principal scope; the existing owner scopes the raw
    # key against this concrete path and commits the resolution plus receipt.
    return await post_notice_resolve(agent_id, notice_id, body, request, idempotency_key)


@router.patch("/api/agents/{agent_id}/notices/current/guarded-v1")
async def edit_current(
    agent_id: int,
    body: GuardedNoticeEdit,
    request: Request,
    key: Annotated[str, Depends(operation_key)],
) -> NoticeItem:
    """Return the immutable acceptance of an observed global notice edit."""
    acceptance = await asyncio.to_thread(
        mutate, request.app.state.db_pool, request.url.path, key, agent_id, body
    )
    record = acceptance.record
    if not acceptance.replayed:
        if record.require_response:
            await asyncio.to_thread(
                _publish_response_required_hint, request.app.state.bus, agent_id
            )
        await lifecycle.publish_notice_posted(
            request.app.state.bus,
            agent_id,
            record.id,
            record.priority,
            record.title,
            record.task_id,
        )
    return record


@router.post("/api/agents/{agent_id}/notices/current/dismiss/guarded-v1")
async def dismiss_current(
    agent_id: int,
    body: ObservedNotice,
    request: Request,
    key: Annotated[str, Depends(operation_key)],
) -> NoticeItem:
    """Withdraw only the observed global row and replay its original acceptance."""
    acceptance = await asyncio.to_thread(
        mutate, request.app.state.db_pool, request.url.path, key, agent_id, body
    )
    record = acceptance.record
    if not acceptance.replayed:
        if record.require_response:
            await asyncio.to_thread(
                _publish_response_required_hint, request.app.state.bus, agent_id
            )
        await lifecycle.publish_notice_resolved(request.app.state.bus, agent_id, record.id)
    return record
