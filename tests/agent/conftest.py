"""Shared fixtures for tests/agent/.

`fake_cancel_event` replaces _llm_cancel / _exec's `subscribe_interrupt` — lets tests
trigger the cancel race directly via `event.set()`, avoiding a real DB inbound
watcher (slow + flaky). The production path always goes through RAII subscribe
(inbound Redis pub/sub); this fixture only affects name bindings in the import
path, with zero impact on production.

Placed in conftest rather than in individual test files: tests/agent/test_cancel.py
verifies the race trigger; tests/agent/test_graph_stream.py runs llm/exec nodes
without wanting a real Redis SUBSCRIBE (fake_redis is an AsyncMock, no pubsub
behavior).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import pytest


@pytest.fixture
def fake_cancel_event(monkeypatch: pytest.MonkeyPatch) -> asyncio.Event:
    event = asyncio.Event()

    @asynccontextmanager
    async def fake_subscribe(_pool, _agent_id) -> AsyncGenerator[asyncio.Event]:
        yield event

    monkeypatch.setattr("agent.graph._llm_cancel.subscribe_interrupt", fake_subscribe)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("agent.graph._exec.subscribe_interrupt", fake_subscribe)  # pyright: ignore[reportUnknownArgumentType]
    return event


@pytest.fixture(autouse=True)
def _fresh_snapshot_cursor(monkeypatch: pytest.MonkeyPatch) -> None:
    """`_node_log._SNAPSHOT_CURSOR` is per-process module state — in production
    one host serves many agents and a test session outlives every test, so the
    cursor set by one test would silently switch a later test's node enter to
    the incremental path. Autouse: every test starts with a clean cursor (first
    enter = full-window snapshot, matching a fresh agent process)."""
    from agent.graph import _node_log

    monkeypatch.setattr(_node_log, "_SNAPSHOT_CURSOR", {})


@pytest.fixture(autouse=True)
def _fresh_unresolved_skill_warnings(monkeypatch: pytest.MonkeyPatch) -> None:
    """`_capabilities._warned_unresolved` suppresses a repeat warning for a
    configured skill name that matched nothing — per process, because the drift
    check re-resolves before every LLM call. In a test session that process
    outlives every test, so whichever test warns first would silence the next
    one's assertion. Autouse: the leak is invisible at the call site, and any
    test that renders the index or resolves a config list can trip it."""
    from agent.graph import _capabilities

    monkeypatch.setattr(_capabilities, "_warned_unresolved", set())  # pyright: ignore[reportUnknownArgumentType]
