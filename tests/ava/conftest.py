"""Shared fixtures for PTY-backed SDK tests (`ava.shell`, `ava.watcher`).

Sessions are pty sessions, each carried by its own detached host process
(`shared.pty_sessions`) under the tmp test home; the `_pty_sessions_env`
fixture pins the env hosts need and sweeps leaked sessions at session end.
Parallel xdist workers each use a reserved high-range fake agent-id
(`_TEST_AGENT_BASE`) for isolation; tests clean up only their own
agent-prefixed sessions via prefix-scoped `kill_all`.

Session naming format: `ava-agent-{agent_id}-shell-{session_id}` (no cluster
segment — the per-home run/pty namespace scopes the sessions, so names are
deterministic by construction).
"""

import os
import re
import time
from collections.abc import Iterator

import pytest
from pydantic import SecretStr

import ava
from ava import shell
from shared.config import settings as _settings

# Parallel xdist worker isolation: pty session records/sockets live under each
# worker's own tmp test home, so workers cannot collide; still, each worker uses
# a reserved high-range fake agent-id (900000+) for its OWN identity in pty
# session tests that create no DB agent;
# the band sits far above the monotonic spawn sequence (see the id contract in
# decisions/2026-06-30-monotonic-test-ids.md), so it never collides with a real spawn or a
# captured self id. Spaced by 10 to leave room for the "other agent" in filter tests.
_WORKER_NUM = int(re.sub(r"\D", "", os.environ.get("PYTEST_XDIST_WORKER", "")) or "0")
_TEST_AGENT_BASE = 900_000 + _WORKER_NUM * 10


@pytest.fixture(autouse=True)
def _mock_api_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set all API keys to dummy values so spawn validation passes in tests
    that exercise the gateway (e.g. test_agents_sdk)."""
    for attr in (
        "anthropic_api_key",
        "deepseek_api_key",
        "gemini_api_key",
        "openai_api_key",
        "xiaomi_api_key",
        "moonshot_api_key",
        "zhipu_api_key",
        "xai_api_key",
    ):
        monkeypatch.setattr(_settings.lm, attr, SecretStr("sk-test"))


def _ensure_agents_meta_row(agent_id: int | None = None) -> None:
    """Ensure agents_meta has a row for the current agent (shell.new() reads session_index).

    Resets session_index to 0 so each test starts from a clean slate.
    For non-local agents (e.g. 999) seeds the agents row first (FK constraint).

    Refuses to run against the production database (bare name "ava") —
    this fixture must only touch a cluster-scoped test database ("ava_<cluster>").
    """
    import psycopg

    from ava._settings import DB_URL
    from shared.test_db_guard import assert_test_db_url

    # Guard: refuse to write to anything but a throwaway test database. This
    # fixture writes synthetic agent rows (spawner="test", high-range IDs) that
    # would pollute the main cluster — the 2026-08-12 incident wrote rows with
    # ids 900002-900010 into the production agents table. The rule lives in
    # shared/test_db_guard.py (single source of truth, shared with the
    # session-start guard in tests/conftest.py and MyAva's test bootstrap).
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


@pytest.fixture(scope="session")
def _pty_sessions_env() -> Iterator[None]:
    """End-of-session sweep for pty-backed tests.

    There is no supervisor daemon to bootstrap (each `new` spawns the
    session's own detached host, shared/pty_sessions; the hosts inherit the
    root conftest's `AVA_CONFIG_FETCH=skip` pin from this process's env). The
    one job left is teardown: kill every session still alive under the tmp
    test home — hosts are detached to init, so a leaked one would survive
    the tmp home and keep running (the 2026-07-24 leaked-daemon outage
    class, now per session instead of per supervisor).
    """
    yield
    from shared.pty_sessions import cli as pty_cli

    for name in list(pty_cli.live_sessions()):
        try:
            pty_cli.session_request(name, {"op": "kill"})
        except OSError:
            pty_cli._kill_by_record(name)


@pytest.fixture
def _isolated_agent(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Give this worker a reserved fake agent-id and clean only its own prefixed sessions.

    Opt-in (NOT autouse): only the pty-backed session tests want this. It mutates
    the process-global `ava.self.AGENT_ID`, so applying it directory-wide would clobber
    DB tests (`test_core`, `test_self_update`, `test_agents_sdk`) that rely on the
    real `ava.self.AGENT_ID` / a `spawn_agent()`-created row. The pty test modules pull
    it in via `pytestmark = pytest.mark.usefixtures("_isolated_agent")` (and the
    `_pty_sessions_env` fixture they also use).

    Each session lives in its own detached host, so cleanup uses prefix-scoped
    `kill_all` (session kill) — there is no daemon-wide teardown. Cleans before
    and after each test to guarantee a clean starting state.

    The agent-id swap goes through monkeypatch, NOT a manual save/restore:
    a test in the module may itself monkeypatch `_agent_id` (test_watcher's
    remind tests), and the per-test monkeypatch instance is shared across all
    requesters with LIFO undo — a manual restore here can run BEFORE that
    instance's undo, which would then re-leak _TEST_AGENT_BASE into every
    later test in the process (resolving workspaces under the wrong id).
    monkeypatch also restores when teardown kill_all raises.
    """
    monkeypatch.setattr(ava._boot, "_agent_id", _TEST_AGENT_BASE)
    shell.kill_all()  # pure sessions, no DB — session tests that need a meta row ensure it themselves
    # A killed session lingers a beat after kill_all() returns. The fake
    # agent-id is fixed per worker and tests reuse session names (e.g.
    # "test-launch"), so under `-n auto` a not-yet-reaped session collides with
    # the next test's create (the test_watcher CI flake class). Wait until our
    # prefix is actually empty before yielding (best-effort, ~5s cap — a stuck
    # session still proceeds).

    for _ in range(250):
        if not shell.list():
            break
        time.sleep(0.02)
    try:
        yield
    finally:
        shell.kill_all()


@pytest.fixture
def _agent_row(_isolated_agent: None) -> int:
    """Seed this worker's fake agent into agents_meta (session_index reset to 0).

    Depends on `_isolated_agent` so `ava.self.AGENT_ID` is already the worker's fake id
    when we seed the row (and so the row is seeded after session cleanup, not before).

    `shell.new()` / `watcher.launch()` read agents_meta.session_index to allocate
    the next session id; this fixture guarantees the row exists and starts the
    counter at 0 so each test has a deterministic, independent starting point.
    Returns the agent id in use.
    """
    _ensure_agents_meta_row()
    return ava.self.AGENT_ID
