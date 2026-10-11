"""`POST /api/agents/{agent_id}/reply-suggestion`: the drafted reply for the agent's last message."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from fastapi import APIRouter, Depends, Request

from gateway.agents.eval_guard import deny_isolated_result_read
from gateway.agents.reply_suggestion.schemas import ReplySuggestionResponse
from gateway.agents.reply_suggestion.service import ReplySuggestionService

router = APIRouter()

# How often a pending suggestion checks whether the client is still there.
DISCONNECT_POLL_S = 0.25


class _ClientGoneError(Exception):
    """Raised by the watcher to cancel the work in its task group."""


async def run_until_disconnect[T](
    work: Awaitable[T], is_disconnected: Callable[[], Awaitable[bool]]
) -> T | None:
    """Await `work`; cancel it and return None as soon as the client goes away.

    A closed tab or an aborted fetch must stop the model calls it started, not let them finish
    and bill. A client that stays connected gets the work's own result or exception.
    """
    outcome: list[T] = []

    async def watch() -> None:
        while not await is_disconnected():
            await asyncio.sleep(DISCONNECT_POLL_S)
        raise _ClientGoneError

    async def run(watcher: asyncio.Task[None]) -> None:
        outcome.append(await work)
        watcher.cancel()

    try:
        async with asyncio.TaskGroup() as group:
            watcher = group.create_task(watch())
            group.create_task(run(watcher))
    except BaseExceptionGroup as failure:
        if failure.subgroup(_ClientGoneError) is not None:
            return None
        # One child failed (the work); surface its own exception, not the group.
        raise failure.exceptions[0] from None
    return outcome[0]


@router.post(
    "/api/agents/{agent_id}/reply-suggestion",
    dependencies=[Depends(deny_isolated_result_read)],
)
async def post_reply_suggestion(agent_id: int, request: Request) -> ReplySuggestionResponse:
    """Draft the reply the user is likely to send to the agent's last message.

    200 with `suggestion` null and a `reason` for every way there is nothing to show: the
    agent is mid-turn, its last message is not waiting for the user, or the gate or draft
    model failed (logged as a warning). 404 for an unknown agent; 503 when the conversation
    cannot be read. The work stops when the client disconnects.
    """
    service: ReplySuggestionService = request.app.state.reply_suggestions
    result = await run_until_disconnect(service.suggest(agent_id), request.is_disconnected)
    # A disconnected client never reads this body.
    return result if result is not None else ReplySuggestionResponse(reason="client_disconnected")
