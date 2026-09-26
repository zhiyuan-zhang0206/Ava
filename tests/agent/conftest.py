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

import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from agent.graph._interrupt import InterruptEvent
from shared.config import settings
from shared.test_db_guard import assert_test_db_url
from tests._containers import grant_runner_login


@pytest.fixture
def fake_cancel_event(monkeypatch: pytest.MonkeyPatch) -> InterruptEvent:
    event = InterruptEvent()

    @asynccontextmanager
    async def fake_subscribe(
        _pool: AsyncConnectionPool | None, _agent_id: int
    ) -> AsyncGenerator[InterruptEvent]:
        yield event

    monkeypatch.setattr("agent.graph._llm_cancel.subscribe_interrupt", fake_subscribe)
    monkeypatch.setattr("agent.graph._exec.subscribe_interrupt", fake_subscribe)
    monkeypatch.setattr("agent.hooks.compact.subscribe_interrupt", fake_subscribe)
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


@pytest.fixture
def runner_exec_env(db_conn: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch) -> None:
    """Give real exec children the launcher's runner projection, preserving setup authority."""
    url = settings.data_plane.db_url
    assert_test_db_url(url, context="real agent exec fixture")
    password = "impersonation-test-runner-password"  # noqa: S105 — private test DB only
    runner_url = grant_runner_login(
        url, owner="ava_citest", login="ava_g0_runner", password=password
    )
    # The real exec child builds its environment from the live os.environ (agent/graph/_exec_subprocess.py),
    # not from the Settings singleton, so the raw-env seam (not monkeypatch.setenv) is the one that reaches it.
    monkeypatch.setitem(os.environ, "AVA_DB_URL", runner_url)
    assert db_conn.info.user != "ava_g0_runner"
