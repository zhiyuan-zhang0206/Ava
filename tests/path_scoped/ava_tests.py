"""Shared fixtures for the ava SDK tests (registered by `tests/fixtures/path_scopes.py`), PTY-backed ones among them (`ava.shell`, `ava.watcher`).

Sessions are pty sessions held by a real pty-sessions service under the tmp test
home (the `pty_service` fixture, imported below), which closes whatever a test
leaves alive when it ends. Parallel xdist workers each use a reserved high-range
fake agent-id (`_TEST_AGENT_BASE`) for isolation; tests clean up only their own
agent-prefixed sessions via prefix-scoped `kill_all`.

Session naming format: `ava-agent-{agent_id}-shell-{session_id}` (no cluster
segment — the home's own service scopes the sessions, so names are deterministic
by construction).
"""

import os
import re
import time
from collections.abc import Iterator

import pytest

import ava
from ava import shell
from tests.fixtures.pin_agent import pin_agent

# One definition shared with the gateway and integration modules; imported here so it
# registers for this module's paths.
from tests.path_scoped.api_keys import _mock_api_keys as _mock_api_keys

# Registers `pty_service` for the paths this module governs.
from tests.path_scoped.pty_service import pty_service as pty_service

# Parallel xdist worker isolation: pty session records/sockets live under each
# worker's own tmp test home, so workers cannot collide; still, each worker uses
# a reserved high-range fake agent-id (900000+) for its OWN identity in pty
# session tests that create no DB agent;
# the band sits far above the monotonic spawn sequence (see the id contract in
# docs/decisions/2026-06-30-monotonic-test-ids.md), so it never collides with a real spawn or a
# captured self id. Spaced by 10 to leave room for the "other agent" in filter tests.
_WORKER_NUM = int(re.sub(r"\D", "", os.environ.get("PYTEST_XDIST_WORKER", "")) or "0")
_TEST_AGENT_BASE = 900_000 + _WORKER_NUM * 10


def _ensure_agents_meta_row(agent_id: int | None = None) -> None:
    """Ensure agents_meta has a row for the current agent (shell.sessions.new() reads session_index).

    Resets session_index to 0 so each test starts from a clean slate.
    For non-local agents (e.g. 999) seeds the agents row first (FK constraint).

    Refuses to run against the production database (bare name "ava") —
    this fixture must only touch a cluster-scoped test database ("ava_<cluster>").
    """
    import psycopg

    from ava.sdk_surface.settings import DB_URL
    from base.db.test_db_guard import assert_test_db_url

    # Guard: refuse to write to anything but a throwaway test database. This
    # fixture writes synthetic agent rows (spawner="test", high-range IDs) that
    # would pollute the main cluster — the 2026-08-12 incident wrote rows with
    # ids 900002-900010 into the production agents table. The rule lives in
    # base/db/test_db_guard.py (single source of truth, shared with the
    # session-start guard in tests/fixtures/provisioning.py).
    assert_test_db_url(str(DB_URL), context="_ensure_agents_meta_row")

    aid = agent_id if agent_id is not None else ava.self.AGENT_ID
    with psycopg.connect(DB_URL) as conn, conn.cursor() as cur:
        # Non-local agents may lack an agents row (agents_meta.id → agents.id FK)
        cur.execute(
            "INSERT INTO agents (id) VALUES (%s) ON CONFLICT (id) DO NOTHING",
            (aid,),
        )
        cur.execute(
            "INSERT INTO agents_meta (id, spawner, status, session_index) "
            "VALUES (%s, 'test', 'running', 0) "
            "ON CONFLICT (id) DO UPDATE SET session_index = 0",
            (aid,),
        )
        conn.commit()


@pytest.fixture
def _isolated_agent(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Give this worker a reserved fake agent-id and clean only its own prefixed sessions.

    Opt-in (NOT autouse): only the pty-backed session tests want this. It mutates
    the process-global `ava.self.AGENT_ID`, so applying it directory-wide would clobber
    DB tests (`test_core`, `test_self_update`, `test_agents_sdk`) that rely on the
    real `ava.self.AGENT_ID` / a `spawn_agent()`-created row. The pty test modules pull
    it in via `pytestmark = pytest.mark.usefixtures("pty_service", "_isolated_agent")`.

    Cleanup is prefix-scoped `kill_all` (session kill): the worker's fake agent
    only ever owns its own prefix. Cleans before and after each test to guarantee
    a clean starting state; `pty_service` must be requested first so it outlives
    the final cleanup.

    The pin is undone by the `identity_restore` fixture, which also runs when teardown
    kill_all raises. A test in the module may re-pin the identity, so teardown pins this fake
    agent again before it kills the fake agent's sessions.
    """
    pin_agent(_TEST_AGENT_BASE)
    shell.sessions.kill_all()  # pure sessions, no DB — session tests that need a meta row ensure it themselves
    # A killed session lingers a beat after kill_all() returns. The fake
    # agent-id is fixed per worker and tests reuse session names (e.g.
    # "test-launch"), so under `-n auto` a not-yet-reaped session collides with
    # the next test's create (the test_watcher CI flake class). Wait until our
    # prefix is actually empty before yielding (best-effort, ~5s cap — a stuck
    # session still proceeds).

    for _ in range(250):
        if not shell.sessions.list():
            break
        time.sleep(0.02)
    try:
        yield
    finally:
        pin_agent(_TEST_AGENT_BASE)
        shell.sessions.kill_all()


@pytest.fixture
def _agent_row(_isolated_agent: None) -> int:
    """Seed this worker's fake agent into agents_meta (session_index reset to 0).

    Depends on `_isolated_agent` so `ava.self.AGENT_ID` is already the worker's fake id
    when we seed the row (and so the row is seeded after session cleanup, not before).

    `shell.sessions.new()` / `watcher.launch()` read agents_meta.session_index to allocate
    the next session id; this fixture guarantees the row exists and starts the
    counter at 0 so each test has a deterministic, independent starting point.
    Returns the agent id in use.
    """
    _ensure_agents_meta_row()
    return ava.self.AGENT_ID
