"""DeltaCoalescer (shared/event_coalescer.py) unit tests.

The coalescer buffers per-item streamed delta fragments and flushes ONE
event per item per SSE event window (EVENT_COALESCE_MS = 40ms), so the
producer rate collapses to ~window^-1 regardless of LLM token rate. Tests
use a short window (10ms) so the auto-flush path is exercised without
slowing the suite.
"""

from __future__ import annotations

import asyncio

import pytest

from shared.event_coalescer import DeltaCoalescer


def _coalescer(window_ms: int = 10) -> tuple[DeltaCoalescer, list[tuple[str, str]]]:
    emitted: list[tuple[str, str]] = []

    def emit(key: str, content: str) -> None:
        emitted.append((key, content))

    return DeltaCoalescer(emit, window_ms=window_ms), emitted


async def test_flush_concatenates_fragments_per_key() -> None:
    c, emitted = _coalescer()
    c.append("k", "a")
    c.append("k", "b")
    c.flush()
    assert emitted == [("k", "ab")]


async def test_keys_flush_independently_in_first_append_order() -> None:
    c, emitted = _coalescer()
    c.append("b", "1")
    c.append("a", "2")
    c.append("b", "3")
    c.flush()
    assert emitted == [("b", "13"), ("a", "2")]


async def test_empty_fragments_ignored() -> None:
    c, emitted = _coalescer()
    c.append("k", "")
    c.append("k", "x")
    c.flush()
    assert emitted == [("k", "x")]


async def test_flush_idempotent_and_empty_buffer_safe() -> None:
    c, emitted = _coalescer()
    c.flush()  # empty — no timer, no emit
    c.append("k", "a")
    c.flush()
    c.flush()  # drained — no double emit
    assert emitted == [("k", "a")]


async def test_auto_flush_after_window() -> None:
    c, emitted = _coalescer(window_ms=10)
    c.append("k", "a")
    await asyncio.sleep(0.05)  # > window
    assert emitted == [("k", "a")]


async def test_append_after_auto_flush_starts_new_window() -> None:
    c, emitted = _coalescer(window_ms=10)
    c.append("k", "a")
    await asyncio.sleep(0.05)
    c.append("k", "b")
    await asyncio.sleep(0.05)
    assert emitted == [("k", "a"), ("k", "b")]


async def test_flush_cancels_pending_timer() -> None:
    c, emitted = _coalescer(window_ms=10)
    c.append("k", "a")
    c.flush()  # cancels the window timer
    await asyncio.sleep(0.05)
    assert emitted == [("k", "a")], "no second flush after cancel"


async def test_requires_running_loop_for_append() -> None:
    """append schedules via loop.call_later — the API contract is
    loop-bound (production: llm stream code runs inside the agent's event
    loop). A loop-less append must fail loudly, not silently buffer."""
    c, _ = _coalescer()

    def _append_sync() -> None:
        c.append("k", "a")

    with pytest.raises(RuntimeError):
        await asyncio.to_thread(_append_sync)
