"""The cancellation re-assertion the dispatcher's scan and subscription loop share.

Split out of `dispatcher.py` to keep it inside the file budget.
"""

from __future__ import annotations

import asyncio


def raise_if_cancellation_pending() -> None:
    """Unwind when a cancellation request was swallowed at a library boundary.

    `Task.cancel()` delivers its `CancelledError` exactly once. A delivery that
    landed in psycopg_pool's async connection check used to be absorbed there —
    the pool returned the connection and retried without re-raising (upstream
    psycopg#1345, through psycopg_pool 3.3.1; fixed by upstream #1401 in
    3.3.2) — leaving the task running with an outstanding cancellation nothing
    would ever deliver again. The scan and the subscription loop re-assert it,
    so a cancelled dispatcher still unwinds instead of looping forever while
    its canceller hangs in `await task`.
    """
    task = asyncio.current_task()
    if task is not None and task.cancelling():
        raise asyncio.CancelledError
