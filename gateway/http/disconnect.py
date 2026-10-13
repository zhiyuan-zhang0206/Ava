"""Stop a request's work when its client goes away."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable

# How often pending work checks whether the client is still there.
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
    finished = asyncio.Event()

    # The watcher ends on `finished`, not only on being cancelled: Starlette's `is_disconnected`
    # runs inside an anyio cancel scope that can absorb a cancellation arriving while it polls,
    # and a watcher that went on polling would keep the group, and so the request, open.
    async def watch() -> None:
        while not finished.is_set():
            if await is_disconnected():
                raise _ClientGoneError
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(finished.wait(), DISCONNECT_POLL_S)

    async def run() -> None:
        try:
            outcome.append(await work)
        finally:
            finished.set()

    try:
        async with asyncio.TaskGroup() as group:
            group.create_task(watch())
            group.create_task(run())
    except BaseExceptionGroup as failure:
        if failure.subgroup(_ClientGoneError) is not None:
            return None
        # One child failed (the work); surface its own exception, not the group.
        raise failure.exceptions[0] from None
    return outcome[0]
