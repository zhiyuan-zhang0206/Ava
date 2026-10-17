# meiva:ref services/derived/insights/run_timeline/docs/range-projection.meiva.okf.md#indexed-history-windows
from __future__ import annotations

import threading
import time
from bisect import bisect_right
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

from fastapi import HTTPException
from psycopg import Connection
from psycopg_pool import ConnectionPool

from base.agents.history.checkpoint import FullHistory
from base.agents.history.timeline_inputs import TimelineReadInputs
from base.db import Database
from services.derived.insights.run_timeline.history import HistoryView
from services.derived.insights.run_timeline.projection.derive import project_segment, segment_view
from services.derived.insights.run_timeline.projection.jobs import IndexJobs
from services.derived.insights.run_timeline.projection.model import VERSION, Projection, Segment
from services.derived.insights.run_timeline.projection.nodes import window_nodes
from services.derived.insights.run_timeline.projection.source import Source, capture_source
from services.derived.insights.run_timeline.projection.store import segments_for_span, window_blocks
from services.derived.insights.run_timeline.schemas import RunTimelineNode, RunTimelineUnit

MAX_AGE_SECONDS = 600.0
MAX_ENTRIES = 16
TTL_SECONDS = 5.0


@dataclass(frozen=True)
class Snapshot:
    agent_id: int
    id: str
    created: float
    head: str | None
    previous: Segment | None
    tail: Projection | None
    lifetime: tuple[datetime, datetime] | None

    @property
    def sealed(self) -> int:
        return self.previous.session + 1 if self.previous else 0

    @property
    def sessions(self) -> int:
        return self.sealed + int(self.tail is not None)

    @property
    def count(self) -> int:
        return (
            self.tail.segment.message_end
            if self.tail
            else self.previous.message_end
            if self.previous
            else 0
        )


class TimelineReader:
    def __init__(
        self,
        db: Database,
        pool: ConnectionPool[Any],
        *,
        inputs: TimelineReadInputs,
        jobs: IndexJobs,
        read_segment: Callable[[Database, int, str], FullHistory],
        read_source: Callable[[Connection[Any], int], Source] = capture_source,
        timer: Callable[[], float] = time.monotonic,
    ) -> None:
        self.db, self.pool, self.inputs, self.jobs = db, pool, inputs, jobs
        self.read_segment, self.timer = read_segment, timer
        self.read_source = read_source
        self._lock = threading.Lock()

        self._building = [threading.Lock() for _ in range(MAX_ENTRIES)]
        self._entries: dict[int, Snapshot] = {}
        self._checked: dict[int, float] = {}

    def get(self, agent_id: int, snapshot: str | None = None) -> Snapshot:
        with self._lock:
            hit = self._entries.get(agent_id)
            valid = hit is not None and self.timer() - hit.created <= MAX_AGE_SECONDS
            if snapshot is not None:
                if valid and hit is not None and hit.id == snapshot:
                    return hit
                raise HTTPException(409, "timeline snapshot expired; refresh the timeline")
            if valid and hit is not None and self.timer() - self._checked[agent_id] <= TTL_SECONDS:
                return hit
        with self._building[agent_id % MAX_ENTRIES]:
            return self._capture(agent_id)

    def _capture(self, agent_id: int) -> Snapshot:
        with self._lock:
            hit = self._entries.get(agent_id)
            if (
                hit is not None
                and self.timer() - hit.created <= MAX_AGE_SECONDS
                and self.timer() - self._checked[agent_id] <= TTL_SECONDS
            ):
                return hit
        with self.pool.connection() as conn:
            source = self.read_source(conn, agent_id)
            if not source.ready:
                state = self.jobs.request(agent_id)
                raise HTTPException(
                    500 if state.status == "failed" else 503,
                    {"code": "timeline_index_" + state.status, "message": state.detail},
                    headers={"Retry-After": "2"},
                )
            if (
                hit is not None
                and hit.head == source.head
                and hit.sealed == source.boundaries
                and self.timer() - hit.created <= MAX_AGE_SECONDS
            ):
                with self._lock:
                    if self._entries.get(agent_id) is hit:
                        self._checked[agent_id] = self.timer()
                return hit
            extent = conn.execute(
                "SELECT min(start_ts), max(end_ts) FROM run_timeline_segments"
                " WHERE agent_id = %s AND version = %s AND session < %s",
                (agent_id, VERSION, source.boundaries),
            ).fetchone()
        result = self._snapshot(agent_id, source, extent)
        with self._lock:
            self._entries.pop(agent_id, None)
            self._entries[agent_id] = result
            self._checked[agent_id] = self.timer()
            while len(self._entries) > MAX_ENTRIES:
                evicted = next(iter(self._entries))
                self._entries.pop(evicted)
                self._checked.pop(evicted)
        return result

    def _snapshot(
        self, agent_id: int, source: Source, extent: tuple[datetime | None, datetime | None] | None
    ) -> Snapshot:
        tail = None
        if source.head is not None and source.head != source.boundary:
            tail = project_segment(
                source.head,
                self.read_segment(self.db, agent_id, source.head),
                source.previous,
                inputs=self.inputs,
            )
        extents: list[tuple[datetime, datetime]] = []
        if extent and extent[0] is not None and extent[1] is not None:
            extents.append((extent[0], extent[1]))
        if tail is not None and tail.view.extent:
            extents.append(tail.view.extent)
        lifetime = (min(e[0] for e in extents), max(e[1] for e in extents)) if extents else None
        if lifetime is not None and lifetime[0] == lifetime[1]:
            lifetime = (lifetime[0], lifetime[1] + timedelta(seconds=1))
        return Snapshot(
            agent_id, uuid4().hex, self.timer(), source.head, source.previous, tail, lifetime
        )

    def window(
        self,
        snapshot: Snapshot,
        start: datetime,
        end: datetime,
        max_units: int | None,
    ) -> tuple[list[RunTimelineNode], list[RunTimelineUnit], int]:
        tail = snapshot.tail
        tail_blocks = [b for b in tail.blocks if b.start <= end and b.end >= start] if tail else []
        with self.pool.connection() as conn:
            nodes = window_nodes(
                self.db,
                conn,
                snapshot.agent_id,
                snapshot.sealed,
                snapshot.count,
                tail.facts if tail else [],
                start,
                end,
            )
            blocks, count = window_blocks(
                conn,
                snapshot.agent_id,
                snapshot.sealed,
                start,
                end,
                allowance=max_units - len(tail_blocks) if max_units is not None else None,
            )
        count += len(tail_blocks)
        if max_units is not None and count > max_units:
            return nodes, [], count
        return nodes, self._parents([*blocks, *tail_blocks], nodes), 0

    @staticmethod
    def _parents(
        blocks: list[RunTimelineUnit], nodes: list[RunTimelineNode]
    ) -> list[RunTimelineUnit]:
        leaves = [n for n in nodes if n.level == 1]
        starts = [n.span_start for n in leaves]
        result: list[RunTimelineUnit] = []
        for block in blocks:
            index = bisect_right(starts, block.i0) - 1
            leaf = leaves[index] if index >= 0 and block.i0 <= leaves[index].span_end else None
            result.append(block.model_copy(update={"parent": leaf.id if leaf else None}))
        return result

    def span(self, snapshot: Snapshot, start: int, end: int) -> list[tuple[Segment, HistoryView]]:
        if end >= snapshot.count:
            raise HTTPException(404, f"message {end} not found: history has {snapshot.count}")
        with self.pool.connection() as conn:
            segments = segments_for_span(conn, snapshot.agent_id, snapshot.sealed, start, end)
        result = [
            (
                segment,
                segment_view(
                    self.read_segment(self.db, snapshot.agent_id, segment.checkpoint_id),
                    segment.read_before,
                    inputs=self.inputs,
                ),
            )
            for segment in segments
        ]
        tail = snapshot.tail
        if (
            tail is not None
            and tail.segment.message_start <= end
            and tail.segment.message_end > start
        ):
            result.append((tail.segment, tail.view))
        return result

    def request(self, snapshot: Snapshot, at: int) -> tuple[int, datetime, Segment, HistoryView]:
        with self.pool.connection() as conn:
            args = (snapshot.agent_id, VERSION, snapshot.sealed)
            following = conn.execute(
                "SELECT idx, request_ts FROM run_timeline_message_facts WHERE agent_id = %s"
                " AND version = %s AND session < %s AND request_ts IS NOT NULL AND idx >= %s"
                " ORDER BY idx LIMIT 1",
                (*args, at),
            ).fetchone()
            previous = conn.execute(
                "SELECT idx, request_ts FROM run_timeline_message_facts WHERE agent_id = %s"
                " AND version = %s AND session < %s AND request_ts IS NOT NULL"
                " ORDER BY idx DESC LIMIT 1",
                args,
            ).fetchone()
        tail = (
            [(f.idx, f.request_ts) for f in snapshot.tail.facts if f.request_ts is not None]
            if snapshot.tail
            else []
        )
        after = [item for item in tail if item[0] >= at] + ([following] if following else [])
        before = tail + ([previous] if previous else [])
        chosen = (
            min(after, key=lambda item: item[0])
            if after
            else max(before, key=lambda item: item[0])
            if before
            else None
        )
        if chosen is None:
            raise HTTPException(404, "the agent has made no LLM request")
        segment, view = self.span(snapshot, chosen[0], chosen[0])[0]
        return chosen[0], chosen[1], segment, view
