"""Shared isolation fixtures for agent tests.

Registered by `tests/fixtures/path_scopes.py`. The cancel-race fixture lives
in `agent.graph.llm.tests.cancel_fixture` and is registered as an opt-in root pytest plugin.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest

from base.config import settings
from base.db.test_db_guard import assert_test_db_url
from tests._containers import grant_runner_login


@pytest.fixture(autouse=True)
def _fresh_snapshot_cursor(monkeypatch: pytest.MonkeyPatch) -> None:
    """`node_log._SNAPSHOT_CURSOR` is per-process module state — in production
    one host serves many agents and a test session outlives every test, so the
    cursor set by one test would silently switch a later test's node enter to
    the incremental path. Autouse: every test starts with a clean cursor (first
    enter = full-window snapshot, matching a fresh agent process)."""
    from agent.graph import node_log

    monkeypatch.setattr(node_log, "_SNAPSHOT_CURSOR", {})


@pytest.fixture(autouse=True)
def _fresh_unresolved_skill_warnings() -> Iterator[None]:
    """`capabilities._warned_unresolved` suppresses a repeat warning for a
    configured skill name that matched nothing — per process, because the drift
    check re-resolves before every LLM call. In a test session that process
    outlives every test, so whichever test warns first would silence the next
    one's assertion. Autouse: the leak is invisible at the call site, and any
    test that renders the index or resolves a config list can trip it."""
    from agent.graph.prompt import capabilities

    capabilities.forget_unresolved_warnings()
    yield
    capabilities.forget_unresolved_warnings()


@pytest.fixture
def runner_exec_env(db_conn: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch) -> None:
    """Give real exec children the launcher's runner projection, preserving setup authority."""
    url = settings.data_plane.db_url
    assert_test_db_url(url, context="real agent exec fixture")
    password = "impersonation-test-runner-password"  # noqa: S105 — private test DB only
    runner_url = grant_runner_login(
        url, owner="ava_citest", login="ava_g0_runner", password=password
    )
    # The real exec child builds its environment from the live os.environ (agent/graph/exec/_subprocess.py),
    # not from the Settings singleton, so the raw-env seam (not monkeypatch.setenv) is the one that reaches it.
    monkeypatch.setitem(os.environ, "AVA_DB_URL", runner_url)
    assert db_conn.info.user != "ava_g0_runner"
