"""Gates the pending-wake scan consults before it cancels a turn or keeps unwinding.

Split out of `dispatcher.py` to keep it inside the file budget: each is a small pure check over
the turn-progress clock, the database-wait ledger or the current task.
"""

from __future__ import annotations

import asyncio

from agent.turn.progress import admission_wait_age_s, turn_progress_snapshot
from base.agents.observation.db_wait import DatabaseWaits


def database_waiting(database_waits: DatabaseWaits, agent_id: int) -> bool:
    progress = turn_progress_snapshot(agent_id)
    last = progress["last_marks"][-1] if progress is not None else None
    return database_waits.snapshot(agent_id, last_progress=last) is not None


def admission_waiting(agent_id: int) -> bool:
    """True while the agent's turn queues at the admission gate — not a stall.

    A queued turn shows no progress by design; cancelling it would only send its
    next attempt to the tail of the queue (a successor is a new ticket),
    punishing the oldest waiters. Wait length is observability (host stats +
    host_admission_wait_exceeded), never a cancellation trigger.
    """
    return admission_wait_age_s(agent_id) is not None


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
