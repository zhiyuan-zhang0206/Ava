"""Streaming output plumbing for the exec node: capture the exec child's
stdout/stderr and publish it incrementally for live frontend display.

`StreamingTextIO` is the sink `contextlib.redirect_stdout/stderr` writes into —
bounded by `exec_output_accumulation_max_chars`, so a runaway print loop is
truncated head+tail as it streams instead of growing the agent process until it
is OOM-killed; `ExecOutputChunkPublisher` ships the accumulated increments to
the per-process event publisher (a non-blocking enqueue). Both are polled by the exec node's
main task on a fixed interval — they hold no exec control-flow state, so they
live apart from the node's thread/cancel/dispatch logic. Emitting (rather than
awaiting Redis) is what keeps the poll loop free to watch cancel/deadline even
when the central Redis is slow.
"""

from __future__ import annotations

import io
import threading
import time
from collections import deque
from dataclasses import dataclass

from shared.config import settings
from shared.event_publisher import AgentEventPublisher
from shared.live_events import ExecOutputChunk

# Spliced in where the accumulation budget dropped the middle, so a reader of
# the retained text sees the gap at the position it happened. It sits between
# the kept head and the kept tail — exactly the region `truncate_both_ends`
# drops downstream, which is why the same fact also travels structurally as a
# `StreamCap` for the envelope's banner.
_DROP_MARKER = (
    "\n\n[... {dropped:,} chars dropped here DURING execution: output exceeded the "
    "{budget:,}-char accumulation budget ({produced:,} chars produced in total). "
    "The dropped middle was never stored anywhere ...]\n\n"
)

# Pushed once to the live stream when the budget is breached. Past that point
# the retained text is a rolling window rather than an append-only buffer, so
# there is no further increment to stream; the frontend picks up the head+tail
# envelope when ExecOutput upserts on completion.
_LIVE_CAP_NOTICE = (
    "\n[... output budget reached — the rest is truncated during execution; "
    "the completed result shows the head + tail ...]\n"
)

# Keep silent execs visibly alive in the frontend at roughly 2Hz.
KEEPALIVE_INTERVAL_S = 0.5


@dataclass(frozen=True)
class StreamCap:
    """Record that the accumulation budget dropped the middle of an exec's output.

    Travels with the text through the `_ExecResult` sum type into
    `_exec_output.wrap_code_output`, which needs both numbers to stay honest:
    `produced_chars` is the TRUE pre-truncation length the instrumentation log
    line reports, and the overflow archive can no longer claim to hold the full
    output — the middle is gone before the archive is ever written.
    """

    produced_chars: int
    budget_chars: int


class StreamingTextIO(io.TextIOBase):
    """Receiver for exec-code `print(...)` / sys.stderr.write.

    Accumulates writes under a fixed character budget (thread-safe
    lock-protected — agents rarely write from multiple threads but some stdlib
    logging / threading paths do); the main asyncio task polls every 50ms via
    `take_pending()` to fetch the increment and publish to frontend.

    The budget uses the same head+tail discipline as the downstream envelope:
    the first half is pinned (an overview / a `help()` listing) and the last
    half is a rolling window (the result / traceback), so a runaway print loop
    is bounded in memory instead of growing until the process is OOM-killed.
    Execution is NOT interrupted — the agent gets the truncated output with an
    explicit marker and can self-correct.

    Differences from stdlib `io.StringIO`: writes are lock-protected and
    bounded; exposes `take_pending` for the main task to fetch increments and
    `cap()` for the envelope layer to learn the true produced length.
    """

    def __init__(self, max_chars: int | None = None) -> None:
        super().__init__()
        if max_chars is None:
            max_chars = settings.sandbox.exec_output_accumulation_max_chars
        self._budget = max_chars
        self._head_budget = max_chars // 2
        self._tail_budget = max_chars - self._head_budget
        self._head = io.StringIO()
        self._head_chars = 0
        self._tail: deque[str] = deque()
        self._tail_chars = 0
        self._produced_chars = 0
        self._published_chars = 0
        self._notice_published = False
        self._lock = threading.Lock()

    def writable(self) -> bool:
        return True

    def write(self, s: str) -> int:
        n = len(s)
        with self._lock:
            self._produced_chars += n
            if self._head_chars < self._head_budget:
                take = min(self._head_budget - self._head_chars, n)
                self._head.write(s[:take])
                self._head_chars += take
                s = s[take:]
            if s:
                self._tail.append(s)
                self._tail_chars += len(s)
                self._trim_tail()
        return n

    def _trim_tail(self) -> None:
        """Drop from the left of the rolling tail until it fits its budget.
        Caller holds the lock. A single oversized write is sliced rather than
        dropped whole, so the tail is always exactly its budget once breached."""
        over = self._tail_chars - self._tail_budget
        while over > 0:
            left = self._tail[0]
            if len(left) <= over:
                self._tail.popleft()
                self._tail_chars -= len(left)
                over -= len(left)
            else:
                self._tail[0] = left[over:]
                self._tail_chars -= over
                over = 0

    def _breached(self) -> bool:
        """Caller holds the lock."""
        return self._produced_chars > self._budget

    def take_pending(self) -> str:
        """Main task fetches the incremental text since last take.

        Streaming is append-only by construction, so it stops at the budget:
        once the tail starts rolling, the retained text is no longer an
        extension of what the frontend already has and there is no honest
        increment to push. A single notice goes out instead, and the final
        ExecOutput upsert replaces the item with the head+tail envelope.
        """
        with self._lock:
            if self._breached():
                if self._notice_published:
                    return ""
                self._notice_published = True
                return _LIVE_CAP_NOTICE
            if self._published_chars >= self._head_chars + self._tail_chars:
                return ""
            full = self._head.getvalue() + "".join(self._tail)
            new = full[self._published_chars :]
            self._published_chars = len(full)
            return new

    def getvalue(self) -> str:
        """The retained text — the full output, or head + drop marker + tail
        once the accumulation budget was breached."""
        with self._lock:
            head, tail = self._head.getvalue(), "".join(self._tail)
            if not self._breached():
                return head + tail
            dropped = self._produced_chars - self._head_chars - self._tail_chars
            marker = _DROP_MARKER.format(
                dropped=dropped, budget=self._budget, produced=self._produced_chars
            )
            return head + marker + tail

    def cap(self) -> StreamCap | None:
        """The accumulation-cap record, or None when the output fit the budget."""
        with self._lock:
            if not self._breached():
                return None
            return StreamCap(produced_chars=self._produced_chars, budget_chars=self._budget)


class ExecOutputChunkPublisher:
    """Emits ExecOutputChunk SSE events as the stream accumulates, so the
    frontend can stream display — the exec node's main task polls every 50ms and
    calls `publish(chunk_text)` to push the newly accumulated text from the last
    round through the per-process event publisher (a non-blocking enqueue).

    `item_id` is shared with the final ExecOutput (`f"{exec_msg_idx}.0"`); the
    frontend appends deltas to the same code_output item by id; on completion,
    ExecOutput upserts with the same id, replacing with the envelope-header version.
    """

    def __init__(
        self,
        event_publisher: AgentEventPublisher,
        agent_id: int,
        item_id: str,
    ) -> None:
        self._publisher = event_publisher
        self._agent_id = agent_id
        self._item_id = item_id
        self._last_activity = time.monotonic()

    def publish(self, text: str) -> None:
        if not text:
            return
        self._last_activity = time.monotonic()
        self._publisher.emit(
            ExecOutputChunk(
                agent_id=self._agent_id,
                item_id=self._item_id,
                content=text,
            ).model_dump_json()
        )

    def maybe_keepalive(self) -> None:
        now = time.monotonic()
        if now - self._last_activity < KEEPALIVE_INTERVAL_S:
            return
        self._last_activity = now
        self._publisher.emit(
            ExecOutputChunk(
                agent_id=self._agent_id,
                item_id=self._item_id,
                content="",
                keepalive=True,
            ).model_dump_json()
        )
