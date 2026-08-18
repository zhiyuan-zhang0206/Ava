"""Time-window coalescing for streamed SSE deltas (F2).

LLM streaming emits one delta event per token fragment (50-100/s) and the
frontend renders at most once per SSE event window (40ms / ~25 FPS,
`shared/live_events.py:EVENT_COALESCE_MS`) — the extra events are pure queue
pressure on the publisher's Redis path. `DeltaCoalescer` collapses them at
the emit side: fragments buffered per item are concatenated and ONE event
per item is handed to the publisher per window, so the producer rate
collapses to ~window^-1 regardless of token rate.

Design:

- One timer per burst, scheduled lazily at the first append of a window
  (`loop.call_later`), so an idle handler costs nothing and there is no
  background task to leak on per-call handler teardown.
- `flush()` drains the buffer immediately and cancels the pending timer —
  call it at stream end so the final <window fragments are never lost to
  the window boundary.
- Ordering contract preserved: keys flush in first-append order, fragments
  of a key in arrival order; a key with no fragment in a window simply does
  not appear in that flush.
- Worst-case added latency is one window (~40ms) — inside the ~100ms
  human "instant" budget and invisible against Redis RTT on a degraded
  link.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from shared.live_events import EVENT_COALESCE_MS

# key -> accumulated fragments, insertion-ordered; a flush emits each key's
# concatenated content exactly once.


class DeltaCoalescer:
    """Coalesce per-item streamed deltas into the SSE event window.

    `append(key, fragment)` buffers; a timer scheduled at the first append
    of a window flushes ONE event per key (concatenated content) after
    `window_ms`. `flush()` (call at stream end / teardown) drains any
    remainder and cancels the pending timer — no fragment is ever lost to
    the window boundary.

    Callbacks run synchronously on the event loop (they hand events to
    `AgentEventPublisher.emit`, a non-blocking enqueue), so appending is
    safe from any synchronous stream-processing code.
    """

    def __init__(
        self,
        emit: Callable[[str, str], None],
        *,
        window_ms: int = EVENT_COALESCE_MS,
    ) -> None:
        self._emit = emit
        self._window_s = window_ms / 1000.0
        self._buf: dict[str, list[str]] = {}
        self._timer: asyncio.TimerHandle | None = None

    def append(self, key: str, fragment: str) -> None:
        """Buffer one fragment under `key`. Empty fragments are ignored (a
        no-op flush would still emit an event with empty content)."""
        if not fragment:
            return
        self._buf.setdefault(key, []).append(fragment)
        if self._timer is None:
            self._timer = asyncio.get_running_loop().call_later(self._window_s, self._flush)

    def flush(self) -> None:
        """Drain buffered fragments now (each key -> one emit), cancelling the
        pending window timer. Safe to call with an empty buffer; idempotent."""
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        self._drain()

    def _flush(self) -> None:
        self._timer = None
        self._drain()

    def _drain(self) -> None:
        if not self._buf:
            return
        buf, self._buf = self._buf, {}
        for key, parts in buf.items():
            self._emit(key, "".join(parts))
