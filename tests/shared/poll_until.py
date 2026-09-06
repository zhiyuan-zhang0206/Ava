from __future__ import annotations

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
    """Wait for a condition, reporting its final observed state on timeout.

    Prefer the ``(bool, state)`` form for diagnostic conditions: a plain-bool
    timeout reports only ``last observed state: False``, which loses the live
    payload (actual name / evidence / child set) that distinguishes failure
    modes at the sites this helper was built for.
    """
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
