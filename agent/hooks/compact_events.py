"""Compaction live-run events — the SSE started/terminal pair.

Extracted from `agent/hooks/compact.py` (file line budget; task #3323). Emits
the pair consumed by the frontend's ticking "Compacting" block: `compact_started`
when a forced/auto compaction begins, and exactly one `compact_finished`
(success / failure / replaced) when it ends. The wire contract lives in
`shared/live_events.py`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

from shared.event_publisher import AgentEventPublisher
from shared.live_events import CompactFinished, CompactStarted


def emit_compact_started(
    publisher: AgentEventPublisher | None,
    agent_id: int,
    *,
    mode: Literal["auto", "request"],
) -> str | None:
    """Emit the start of one forced/auto compaction run; returns its
    ``compact_id`` — the pairing key for the terminal `emit_compact_finished`.

    No publisher (container / eval contexts) returns None: callers then carry
    None as the run id and the matching finish is silently skipped — the live
    view is best-effort by design, the durable summary marker is not.
    """
    if publisher is None:
        return None
    compact_id = uuid4().hex
    publisher.emit(
        CompactStarted(
            agent_id=agent_id,
            compact_id=compact_id,
            started_at=datetime.now(UTC).isoformat(),
            mode=mode,
        ).model_dump_json()
    )
    return compact_id


def emit_compact_finished(
    publisher: AgentEventPublisher | None,
    agent_id: int,
    compact_id: str | None,
    *,
    status: Literal["success", "failure", "replaced"],
) -> None:
    """Emit the terminal signal of one compaction run, closing its ticking
    "Compacting" block.

    Every emitted `CompactStarted` reaches exactly one of these: success (the
    summary was generated and applied), failure (every attempt failed), or
    replaced (the outcome was discarded — superseded by a later compact in the
    same batch, or dropped by a co-batched cancel). No publisher / no run id
    is a no-op (see `emit_compact_started`).
    """
    if publisher is None or compact_id is None:
        return
    publisher.emit(
        CompactFinished(
            agent_id=agent_id,
            compact_id=compact_id,
            status=status,
            finished_at=datetime.now(UTC).isoformat(),
        ).model_dump_json()
    )
