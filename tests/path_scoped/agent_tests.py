"""Shared isolation fixtures for agent tests.

Registered by `tests/fixtures/path_scopes.py`. The cancel-race fixture lives
in `agent.graph.llm.tests.cancel_fixture` and is registered as an opt-in root pytest plugin.
"""

from __future__ import annotations

import os
from typing import Any

import psycopg
import pytest

from agent.graph.tests.cursor_fixture import _fresh_snapshot_cursor as _fresh_snapshot_cursor
from base.config import settings
from base.db.test_db_guard import assert_test_db_url
from tests._containers import grant_runner_login


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
