"""Finite database-wait evidence, separate from actual agent node progress."""

import asyncio
import math
import time
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, TypedDict, cast
from uuid import UUID

from shared.runtime_incarnation import RuntimeIncarnation

# Covers two 30s DB-only handoff stages + a heartbeat delayed by 10s ownership
# renewal, 3s publication and 15s sleep, with 12s scheduling allowance. Recovery
# itself needs less: a 5s attempt + 30s backoff before another real attempt.
# Publication does not renew this deadline: only a new bounded attempt does.
DB_WAIT_PROOF_TTL_SECONDS = 100.0
_CLOCK_SKEW_SECONDS = 5.0


class DatabaseWaitProof(TypedDict):
    generation: str
    owner: str
    observed_at: float
    expires_at: float


@dataclass
class DatabaseWait:
    incarnation: RuntimeIncarnation
    task: asyncio.Task[Any]
    deadline: float = 0.0
    observed_at: float = 0.0
    expires_at: float = 0.0
    handoff_at: float | None = None

    def renew(self) -> None:
        """The original task entered another bounded database recovery attempt."""
        self.handoff_at = None
        self.deadline = time.monotonic() + DB_WAIT_PROOF_TTL_SECONDS
        self.observed_at = time.time()
        self.expires_at = self.observed_at + DB_WAIT_PROOF_TTL_SECONDS

    def complete(self) -> None:
        """A finite, non-renewing handoff to the original task's next DB stage."""
        self.renew()
        self.handoff_at = time.monotonic()


_WAITING: dict[int, DatabaseWait] = {}


@contextmanager
def database_wait(incarnation: RuntimeIncarnation) -> Generator[DatabaseWait]:
    """Keep evidence only for this exact live recovery task and incarnation."""
    task = asyncio.current_task()
    previous = _WAITING.get(incarnation.agent_id)
    if task is None or (
        previous is not None
        and (
            previous.task is not task
            or previous.incarnation != incarnation
            or previous.handoff_at is None
        )
    ):
        raise RuntimeError("database recovery requires one original task per agent")
    entry = previous or DatabaseWait(incarnation, task)
    entry.handoff_at = None
    _WAITING[incarnation.agent_id] = entry
    if previous is None:

        def cleanup(_task: asyncio.Task[Any]) -> None:
            if _WAITING.get(incarnation.agent_id) is entry:
                del _WAITING[incarnation.agent_id]

        task.add_done_callback(cleanup)
    try:
        yield entry
    finally:
        if entry.handoff_at is None and _WAITING.get(incarnation.agent_id) is entry:
            del _WAITING[incarnation.agent_id]


def database_wait_snapshot(
    agent_id: int, *, last_progress: float | None = None
) -> DatabaseWaitProof | None:
    """Read without extending evidence; a stuck or ended task loses its grace."""
    entry = _WAITING.get(agent_id)
    if (
        entry is None
        or entry.task.done()
        or entry.task.cancelling()
        or time.monotonic() >= entry.deadline
    ):
        return None
    if (
        entry.handoff_at is not None
        and last_progress is not None
        and last_progress > entry.handoff_at
    ):
        del _WAITING[agent_id]
        return None
    return {
        "generation": str(entry.incarnation.generation),
        "owner": str(entry.incarnation.owner),
        "observed_at": entry.observed_at,
        "expires_at": entry.expires_at,
    }


def database_wait_matches(proof: object, generation: UUID | None, owner: UUID | None) -> bool:
    """Only fresh evidence for the DB candidate's exact owner exempts a stall."""
    if not isinstance(proof, dict) or generation is None or owner is None:
        return False
    proof = cast(dict[str, object], proof)
    if proof.get("generation") != str(generation) or proof.get("owner") != str(owner):
        return False
    observed, expires = proof.get("observed_at"), proof.get("expires_at")
    if any(
        isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
        for value in (observed, expires)
    ):
        return False
    now = time.time()
    return (
        isinstance(observed, (int, float))
        and isinstance(expires, (int, float))
        and 0 < expires - observed <= DB_WAIT_PROOF_TTL_SECONDS
        and observed <= now + _CLOCK_SKEW_SECONDS
        and now < expires
    )
