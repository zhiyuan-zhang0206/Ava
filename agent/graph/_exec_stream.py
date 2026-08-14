"""Streaming output plumbing for the exec node: capture the worker thread's
stdout/stderr and publish it incrementally for live frontend display.

`StreamingTextIO` is the sink `contextlib.redirect_stdout/stderr` writes into;
`ExecOutputChunkPublisher` ships the accumulated increments to the per-process
event publisher (a non-blocking enqueue). Both are polled by the exec node's
main task on a fixed interval — they hold no exec control-flow state, so they
live apart from the node's thread/cancel/dispatch logic. Emitting (rather than
awaiting Redis) is what keeps the poll loop free to watch cancel/deadline even
when the central Redis is slow.
"""

from __future__ import annotations

import io
import threading

from shared.event_publisher import AgentEventPublisher
from shared.live_events import ExecOutputChunk


class StreamingTextIO(io.TextIOBase):
    """Receiver for worker thread `print(...)` / sys.stderr.write.

    Accumulates writes to an internal StringIO (thread-safe lock-protected —
    agents rarely write from multiple threads but some stdlib logging /
    threading paths do); the main asyncio task polls every 50ms via
    `take_pending()` to fetch the increment and publish to frontend.

    Differences from stdlib `io.StringIO`: writes are lock-protected; exposes
    `take_pending` for the main task to fetch increments.
    """

    def __init__(self) -> None:
        super().__init__()
        self._buf = io.StringIO()
        self._published_chars = 0
        self._lock = threading.Lock()

    def writable(self) -> bool:
        return True

    def write(self, s: str) -> int:
        with self._lock:
            return self._buf.write(s)

    def take_pending(self) -> str:
        """Main task fetches the incremental text since last take."""
        with self._lock:
            full = self._buf.getvalue()
            new = full[self._published_chars :]
            self._published_chars = len(full)
            return new

    def getvalue(self) -> str:
        with self._lock:
            return self._buf.getvalue()


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

    def publish(self, text: str) -> None:
        if not text:
            return
        self._publisher.emit(
            ExecOutputChunk(
                agent_id=self._agent_id,
                item_id=self._item_id,
                content=text,
            ).model_dump_json()
        )
