"""Versioned notice selectors requiring observed identity and transactional receipts."""

import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends, Request

from gateway.agents.notice_operations.current import (
    GuardedNoticeEdit,
    ObservedNotice,
    mutate,
    operation_key,
)
from gateway.agents.notices import _publish_response_required_hint
from gateway.agents.schemas import NoticeItem
from ops import lifecycle

router = APIRouter()


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
