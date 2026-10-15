from __future__ import annotations

import asyncio
import time
from collections.abc import Callable

type PollResult = bool | tuple[bool, object]


def _result_state(result: PollResult) -> tuple[bool, object]:
    if isinstance(result, tuple):
        return result
    return result, result


def poll_until(
    condition: Callable[[], PollResult],
    *,
    timeout: float = 30.0,
    interval: float = 0.05,
    what: str = "condition",
) -> None:
    started = time.monotonic()
    deadline = started + timeout
    while time.monotonic() < deadline:
        reached, state = _result_state(condition())
        if reached:
            return
        time.sleep(interval)

    reached, state = _result_state(condition())
    if reached:
        return
    waited = time.monotonic() - started
    raise AssertionError(
        f"Timed out after {waited:.2f}s waiting for {what}; last observed state: {state!r}"
    )


async def poll_until_async(
    condition: Callable[[], PollResult],
    *,
    timeout: float = 30.0,
    interval: float = 0.01,
    what: str = "condition",
) -> None:
    started = time.monotonic()
    deadline = started + timeout
    while time.monotonic() < deadline:
        reached, state = _result_state(condition())
        if reached:
            return
        await asyncio.sleep(interval)

    reached, state = _result_state(condition())
    if reached:
        return
    waited = time.monotonic() - started
    raise AssertionError(
        f"Timed out after {waited:.2f}s waiting for {what}; last observed state: {state!r}"
    )
