# meiva:link services/derived/insights/run_timeline/docs/range-projection.meiva.okf.md
from __future__ import annotations

import logging
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from base.agents.history.checkpoint import FullHistory
from base.agents.history.checkpoint_segment import read_checkpoint_segment
from base.agents.history.timeline_inputs import TimelineReadInputs
from base.db import Database
from base.db.transaction import write_transaction
from services.derived.insights.run_timeline.projection.derive import project_segment
from services.derived.insights.run_timeline.projection.source import next_boundary
from services.derived.insights.run_timeline.projection.store import last_segment, publish

_log = logging.getLogger(__name__)


def index_next(
    db: Database,
    agent_id: int,
    *,
    inputs: TimelineReadInputs,
    read_segment: Callable[[Database, int, str], FullHistory] = read_checkpoint_segment,
) -> bool:
    pool = db.pool(min_size=1, max_size=1)
    try:
        with write_transaction(pool) as conn:
            conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (f"timeline:{agent_id}",)
            )
            previous = last_segment(conn, agent_id)
            checkpoint_id = next_boundary(conn, agent_id, previous)
            if checkpoint_id is None:
                return False
            history = read_segment(db, agent_id, checkpoint_id)
            projection = project_segment(checkpoint_id, history, previous, inputs=inputs)
            publish(conn, agent_id, projection)
        return True
    finally:
        pool.close()


@dataclass(frozen=True)
class Preparation:
    status: Literal["queued", "indexing", "failed", "busy"]
    detail: str


class IndexWorker:
    def __init__(self, run: Callable[[threading.Event], None]) -> None:
        self._body = run
        self._stop_requested = threading.Event()
        self._finished = threading.Event()
        self._failure: BaseException | None = None
        self._thread = threading.Thread(target=self._run, name="timeline-index", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            self._body(self._stop_requested)
        except BaseException as exc:
            self._failure = exc
            _log.exception("timeline index worker exited unexpectedly: %s", exc)
        finally:
            self._finished.set()

    @property
    def failed(self) -> bool:
        return self._finished.is_set() and self._failure is not None

    def close(self) -> bool:
        self._stop_requested.set()
        self._thread.join(timeout=2)
        stopped = not self._thread.is_alive()
        if not stopped:
            _log.warning("timeline index worker still finishing an atomic segment on shutdown")
        if self._failure is not None:
            raise self._failure
        return stopped


class IndexJobs:
    def __init__(self, step: Callable[[int], bool]) -> None:
        self._step = step
        self._condition = threading.Condition()
        self._queue: deque[int] = deque()
        self._states: dict[int, Preparation] = {}
        self._failures: dict[int, Preparation] = {}
        self._worker: IndexWorker | None = None
        self._closed = False

    def request(self, agent_id: int) -> Preparation:
        with self._condition:
            if self._worker is not None and self._worker.failed:
                return Preparation("failed", "timeline index worker stopped; restart insights")
            failure = self._failures.pop(agent_id, None)
            if failure is not None:
                return failure
            state = self._states.get(agent_id)
            if state is not None:
                return state
            if self._closed or len(self._states) >= 16:
                return Preparation("busy", "timeline preparation queue is full; retry shortly")
            state = Preparation("queued", "timeline index is being prepared")
            self._states[agent_id] = state
            self._queue.append(agent_id)
            if self._worker is None:
                self._worker = IndexWorker(self._run)
            self._condition.notify()
            return state

    def close(self) -> bool:
        with self._condition:
            self._closed = True
            self._condition.notify_all()
        return self._worker.close() if self._worker else True

    def _run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            with self._condition:
                self._condition.wait_for(lambda: self._queue or self._closed)
                if self._closed:
                    return
                agent_id = self._queue.popleft()
                self._states[agent_id] = Preparation("indexing", "timeline index is being prepared")
            try:
                more = self._step(agent_id)
            except Exception:
                _log.exception("timeline projection failed for agent %s", agent_id)
                with self._condition:
                    self._states.pop(agent_id, None)
                    self._failures[agent_id] = Preparation(
                        "failed", "timeline preparation failed; retry the timeline"
                    )
                    while len(self._failures) > 16:
                        self._failures.pop(next(iter(self._failures)))
                    self._condition.notify_all()
                continue
            with self._condition:
                if more:
                    self._queue.append(agent_id)
                    self._states[agent_id] = Preparation(
                        "queued", "timeline index is being prepared"
                    )
                else:
                    self._states.pop(agent_id, None)
