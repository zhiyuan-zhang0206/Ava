"""Sessions — the stretches of an agent's history between two compactions, and how much of each the tree describes.

A session is one compaction segment of the stitched checkpoint history (`FullHistory`): number 1 is
the oldest and numbers grow with time, so a number is stable and reproducible while the agent
keeps compacting (a new session is only ever added at the end). A segment closed by a compaction
carries that compaction's boundary checkpoint id; the newest segment, when no boundary closes it,
is the session still in progress (its id is None).

What a session can have described is its *material*: the segment body past its framework head (the
SystemMessage, the one-time notes, the carried-over compact summary) up to the last request the agent
sent (`sendable_len`). Material is covered by the level-1 nodes of the chunk pipeline; a run no node
reaches that holds nothing but framework notes is not counted as missing (the consumer skips such a
chunk). The coverage figures here and the chunk plan of `build.py` come from the same runs.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from langchain_core.messages import AIMessage, BaseMessage
from psycopg_pool import ConnectionPool

from base.agents.history.checkpoint import FullHistory
from base.agents.history.hierarchy.chunk_plan import segment_requests
from base.agents.history.hierarchy.chunks import segment_head_len, sendable_len, uncovered
from base.agents.history.hierarchy.units import divide_units
from base.agents.history.timeline_inputs import TimelineReadInputs

CoverageStatus = Literal["none", "partial", "full"]


class SessionBoundaryError(Exception):
    """The history's segments and the compaction boundary checkpoints do not line up."""


@dataclass(frozen=True)
class Session:
    """One session. `first` / `end` are the segment body's stitched message indices (end exclusive);
    `material` is the inclusive stitched span that can be described, None when there is none."""

    number: int
    segment: int
    boundary_checkpoint_id: str | None
    first: int
    end: int
    start: datetime | None
    ended: datetime | None
    messages: int
    peak_input_tokens: int
    material: tuple[int, int] | None


@dataclass(frozen=True)
class Coverage:
    """How much of a session's material the level-1 nodes describe."""

    status: CoverageStatus
    ratio: float
    covered_messages: int
    total_messages: int


def build_sessions(
    history: FullHistory, boundaries_ascending: Sequence[str], read: Sequence[datetime | None]
) -> list[Session]:
    """The history's sessions, oldest first (number 1 first).

    `boundaries_ascending` are the compaction boundary checkpoint ids oldest first; segment k is
    closed by `boundaries_ascending[k]` when there is one. `read` is the read time of every message
    (`units.read_times`).

    Raises:
        SessionBoundaryError: more boundaries than segments, or the segments outnumber them by more
            than the one still in progress.
    """
    count = len(history.segment_starts)
    if len(boundaries_ascending) > count or count - len(boundaries_ascending) > 1:
        raise SessionBoundaryError(
            f"{count} segments against {len(boundaries_ascending)} compaction boundaries"
        )
    requests = segment_requests(history)
    out: list[Session] = []
    for k in range(count):
        first = history.segment_starts[k]
        end = history.segment_starts[k + 1] if k + 1 < count else len(history.messages)
        offset = 1 if history.segment_heads[k] is not None else 0
        request = requests[k]
        mat_first = first + segment_head_len(request) - offset
        mat_end = first + sendable_len(request) - offset
        times = [t for t in read[first:end] if t is not None]
        out.append(
            Session(
                number=k + 1,
                segment=k,
                boundary_checkpoint_id=(
                    boundaries_ascending[k] if k < len(boundaries_ascending) else None
                ),
                first=first,
                end=end,
                start=min(times) if times else None,
                ended=max(times) if times else None,
                messages=end - first,
                peak_input_tokens=_peak_input_tokens(history.messages[first:end]),
                material=(mat_first, mat_end - 1) if mat_end > mat_first else None,
            )
        )
    return out


def _peak_input_tokens(messages: Sequence[BaseMessage]) -> int:
    return max(
        (
            int(msg.usage_metadata["input_tokens"])
            for msg in messages
            if isinstance(msg, AIMessage) and msg.usage_metadata
        ),
        default=0,
    )


def has_matter(messages: Sequence[BaseMessage], *, timeline_inputs: TimelineReadInputs) -> bool:
    """Whether a run holds anything to describe: something besides framework-injected notes."""
    return any(
        unit.kind != "note"
        for unit in divide_units(list(messages), timeline_inputs=timeline_inputs)
    )


def missing_runs(
    history: FullHistory,
    material: tuple[int, int],
    covered: Sequence[tuple[int, int]],
    *,
    timeline_inputs: TimelineReadInputs,
) -> list[tuple[int, int]]:
    """The inclusive runs of `material` no level-1 node covers and that have something to describe."""
    return [
        run
        for run in uncovered(material, covered)
        if has_matter(history.messages[run[0] : run[1] + 1], timeline_inputs=timeline_inputs)
    ]


def coverage_of(
    history: FullHistory,
    session: Session,
    covered: Sequence[tuple[int, int]],
    *,
    timeline_inputs: TimelineReadInputs,
) -> Coverage:
    """The session's coverage by the sorted level-1 spans `covered`.

    `full` also when there is nothing to describe (no material, or only framework notes are left).
    """
    if session.material is None:
        return Coverage("full", 1.0, 0, 0)
    total = session.material[1] - session.material[0] + 1
    missing = sum(
        b - a + 1
        for a, b in missing_runs(
            history, session.material, covered, timeline_inputs=timeline_inputs
        )
    )
    if missing == 0:
        return Coverage("full", 1.0, total, total)
    if not _touches(session.material, covered):
        return Coverage("none", 0.0, 0, total)
    return Coverage("partial", (total - missing) / total, total - missing, total)


def _touches(material: tuple[int, int], covered: Sequence[tuple[int, int]]) -> bool:
    return any(a <= material[1] and b >= material[0] for a, b in covered)


def load_covered_spans(pool: ConnectionPool, agent_id: int) -> list[tuple[int, int]]:
    """The inclusive message spans of the agent's level-1 nodes of the chunk pipeline, in order."""
    with pool.connection() as conn:
        rows = conn.execute(
            "SELECT span_start, span_end FROM understanding_nodes"
            " WHERE agent_id = %s AND depth = 1 AND engine_version LIKE 'chunk-%%'"
            " ORDER BY span_start",
            (agent_id,),
        ).fetchall()
    return [(int(a), int(b)) for a, b in rows]
