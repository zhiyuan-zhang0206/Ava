"""Best-effort SSE event fan-out for one agent process (F2).

All of an agent's live-view events (chat / reasoning / code deltas, exec
output chunks, timeline snapshots, ...) are published to the central Redis for
the frontend to stream. That Redis sits behind a flaky corp link, and a
publish that blocks the agent's control flow (the exec poll loop that watches
cancel/deadline, the llm stream loop) stalls the whole turn. So publishing is
decoupled here: `emit()` is a synchronous, non-blocking, never-raising enqueue,
and a single background worker drains the queue and publishes serially.

Serial matters: SSE events carry an ordering contract (a Start before its
Deltas, streamed chunks concatenated in order). One FIFO worker preserves it,
where fire-and-forget per-event tasks would reorder under load.

Degradation is intentional and safe: a slow/unreachable Redis makes a publish
time out or error, and that batch of events is dropped (rate-limited warning)
rather than retried or blocked on. The worker drains in batches through a
single Redis pipeline, so a degraded link's round-trip cost is amortized across
many events instead of paid once per event (a link where one publish takes 1s
would otherwise drain at 1 event/s and fill the queue within seconds).

A full queue sheds the OLDEST buffered event to make room for the newest (the
live view is worth the most-recent state; a gap in the past is corrected by the
next authoritative snapshot at the end of every turn). The live view degrades;
committed state (DB checkpoints, files) is unaffected, and the end of every
turn re-publishes an authoritative snapshot that corrects any gap.
"""

from __future__ import annotations

import asyncio
import time
from contextlib import suppress

import redis.asyncio as aredis

from shared.log import logger

_DEFAULT_MAXSIZE = 2048
_DEFAULT_PUBLISH_TIMEOUT_S = 2.0
_DEFAULT_DRAIN_TIMEOUT_S = 2.0
_WARN_INTERVAL_S = 5.0  # rate-limit dropped-event warnings under sustained loss
_BATCH_SIZE = 64  # events drained per pipeline round-trip (see _run)


class AgentEventPublisher:
    """Single-worker FIFO publisher for one agent's best-effort SSE events.

    Lifecycle: build it, `await start()` once, hand it to the graph nodes via
    the run context, and `await aclose()` at process exit (bounded drain, then
    the worker stops). `emit(payload)` is a synchronous enqueue, safe to call
    before start (the event waits in the queue) and after a full queue (the
    newest event is dropped).
    """

    def __init__(
        self,
        redis_async: aredis.Redis,
        events_channel: str,
        *,
        agent_id: int,
        maxsize: int = _DEFAULT_MAXSIZE,
        publish_timeout: float = _DEFAULT_PUBLISH_TIMEOUT_S,
        drain_timeout: float = _DEFAULT_DRAIN_TIMEOUT_S,
    ) -> None:
        self._redis = redis_async
        self._events_channel = events_channel
        self._agent_id = agent_id
        self._publish_timeout = publish_timeout
        self._drain_timeout = drain_timeout
        self._queue: asyncio.Queue[str] = asyncio.Queue(maxsize=maxsize)
        self._task: asyncio.Task[None] | None = None
        self._dropped = 0
        self._reported = 0  # drops already covered by a warning — delta-only reports
        self._last_warn = 0.0
        self._last_drop_kind = "unknown"
        self._last_drop_detail = ""

    def emit(self, payload: str) -> None:
        """Enqueue one already-serialized event for publishing. Synchronous,
        never blocks, never raises — a full queue sheds the OLDEST buffered
        event to keep the newest (the live view is worth the most-recent
        state; the turn-end snapshot repairs the gap) and counts toward a
        rate-limited warning."""
        try:
            self._queue.put_nowait(payload)
        except asyncio.QueueFull:
            # Shed the oldest buffered event, then enqueue the new one. No
            # await between get and put, so the slot freed here is still free.
            # The shedded event must also be task_done()'d: an asyncio.Queue
            # counts unfinished tasks per put, released only by task_done (get
            # does not touch it), and the worker never dequeues a shedded
            # event — without this, join() (aclose's bounded drain) can never
            # complete once the queue has ever been full.
            with suppress(asyncio.QueueEmpty):
                self._queue.get_nowait()
                self._queue.task_done()
            try:
                self._queue.put_nowait(payload)
            except asyncio.QueueFull:
                self._note_drop("queue_full")
                return
            self._note_drop("queue_full")

    async def start(self) -> None:
        """Spawn the single background publish worker. Idempotent."""
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name=f"event-publisher-{self._agent_id}")

    async def aclose(self) -> None:
        """Bounded drain, then stop: give the worker up to drain_timeout to
        flush the queue, then cancel it. Idempotent; safe if start() was never
        called (nothing to cancel)."""
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._queue.join(), timeout=self._drain_timeout)
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        self._flush_warn()

    async def _run(self) -> None:
        while True:
            batch = [await self._queue.get()]
            with suppress(asyncio.QueueEmpty):
                while len(batch) < _BATCH_SIZE:
                    batch.append(self._queue.get_nowait())
            try:
                await asyncio.wait_for(self._publish_batch(batch), timeout=self._publish_timeout)
            except asyncio.CancelledError:
                raise  # aclose cancelled us — propagate to exit the worker
            except Exception as exc:
                # Slow / unreachable central Redis: drop this batch of
                # live-view events rather than stalling the worker behind a
                # hung publish, and tear down the client's connections so the
                # next batch reconnects fresh instead of riding a half-dead
                # socket (keepalive/health-check would take up to ~30s).
                self._note_drop("publish_error", detail=repr(exc))
                with suppress(Exception):
                    # redis-py (8.x) types disconnect() as async and it IS a
                    # coroutine at runtime — it must be awaited or the
                    # tear-down never runs (the coroutine object is created and
                    # discarded, and every failed batch also emits a
                    # "coroutine ... was never awaited" RuntimeWarning). The
                    # await is inside suppress(Exception).
                    await self._redis.connection_pool.disconnect(inuse_connections=True)
            finally:
                for _ in batch:
                    self._queue.task_done()

    async def _publish_batch(self, batch: list[str]) -> None:
        """Publish a whole batch in ONE pipeline round-trip (FIFO preserved:
        commands on a single connection execute in order).

        Command-level failures (e.g. an ACL NOPERM on this channel) shed only
        the failing event — the rest of the batch still goes out, returned as
        Exception objects in the results under `raise_on_error=False`. A
        connection-level failure (socket dead / timed out) raises from
        execute() and sheds the whole batch — there is no way to know which
        commands the server executed, so none are retried; best-effort by
        design, and the turn-end snapshot corrects any gap."""
        pipe = self._redis.pipeline(transaction=False)
        for payload in batch:
            # redis-py types publish()'s **kwargs as Unknown; the call itself is fully typed.
            pipe.publish(self._events_channel, payload)  # pyright: ignore[reportUnknownMemberType]
        results = await pipe.execute(raise_on_error=False)
        failed = [r for r in results if isinstance(r, Exception)]
        if failed:
            self._note_drop("publish_error", detail=repr(failed[0]))

    def _note_drop(self, kind: str, detail: str = "") -> None:
        self._dropped += 1
        self._last_drop_kind = kind
        self._last_drop_detail = detail
        now = time.monotonic()
        if now - self._last_warn >= _WARN_INTERVAL_S:
            self._flush_warn()
            self._last_warn = now

    def _flush_warn(self) -> None:
        # Delta since the last report, not the lifetime total: a burst is
        # reported once per _WARN_INTERVAL_S, and the counter keeps counting
        # between reports, so no drop is ever lost to the rate limit — the
        # final residual is flushed by aclose().
        n = self._dropped - self._reported
        self._reported = self._dropped
        if n <= 0:
            return
        # The latest cause is named so "central redis down" (ConnectionError)
        # is distinguishable from "publish slow" (TimeoutError) and from
        # local backpressure (queue full) without a debugger. The line is
        # also a structured `sse_drop` agent_event: the ops monitor panel
        # counts queue backlog over time from these rows (see
        # gateway/ops_series_lgtm.py). Rate-limited to _WARN_INTERVAL_S, so a
        # sustained burst reports once per interval with the delta n.
        logger.warning(
            "[event-publisher] dropped {n} SSE event(s) (agent_id={aid}, "
            "kind={kind}, last cause: {detail}) — live view degraded, "
            "committed state unaffected",
            event="sse_drop",
            n=n,
            aid=self._agent_id,
            kind=self._last_drop_kind,
            detail=self._last_drop_detail,
            queue_size=self._queue.maxsize,
        )
