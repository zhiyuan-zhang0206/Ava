"""ScheduleManager x real pty-sessions service integration.

A real pty-sessions service + real DB + real backend + the real
`services.wake.schedule_manager.runner` entrypoint: `_launch` creates a session in the service,
`_live_ids` / `capture_blocking` see it, `_reap` tears it down, and a failed
revision is never launched again after manager restart.

The schedule commands are deliberately trivial (`sleep 30` for the live
paths, `false` for the crash paths) — the runner's real script execution and
status semantics are covered by test_schedule_runner.py.
"""

import contextlib
import os
import shlex
import time
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from base.cluster import session_name
from base.deploy.lifecycle.start_serving import RootBirth
from base.sessions.backend import get_shell_backend
from base.sessions.pty import client
from base.sessions.pty.tests.pty_service import PtyServiceProcess
from base.sessions.pty.tests.pty_shells import wait_for
from gateway.schedules import session_control
from services.wake.schedule_manager import manager as sm

REPO = Path(__file__).resolve().parents[4]

pytestmark = [
    pytest.mark.usefixtures("_pty_home"),
]


@pytest.fixture
def _pty_home(
    db_url: str, tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Iterator[str]:
    """A DEDICATED home, with its own pty-sessions service, for the schedule sessions.

    The service is a subprocess pinned to this home; each schedule launch creates a
    session in it. Torn down by closing every session and stopping the service, so
    no shell outlives the test run."""
    home = tmp_path_factory.mktemp("pty-sched-home")
    # The schedule runner builds the full gateway settings from the home's
    # .env; point it at the test database so a `sleep 30` schedule stays up
    # long enough to observe (the runner's own semantics are covered by
    # test_schedule_runner.py).
    from base.config import settings as _settings

    (home / ".env").write_text(f"AVA_DB_URL={_settings.data_plane.db_url}\n")
    monkeypatch.setenv("AVA_HOME", str(home))
    service = PtyServiceProcess(home)
    try:
        service.start()
        yield str(home)
    finally:
        with contextlib.suppress(client.ServiceUnavailableError, client.ServiceError):
            client.close_all(grace_s=0.5, kill_s=2.0)
        service.stop()


@pytest.fixture
def pool(db_url: str, _host_is_serving: None) -> Iterator[ConnectionPool[psycopg.Connection]]:
    from base.config import settings

    p: ConnectionPool[psycopg.Connection] = ConnectionPool(
        settings.data_plane.db_url, min_size=1, max_size=2, open=True
    )
    try:
        yield p
    finally:
        p.close()


def _insert_schedule(
    conn: psycopg.Connection,
    name: str,
    script: str,
    command: str,
    *,
    enabled: bool = True,
) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO schedules (name, script, command, enabled, status) "
            "VALUES (%s, %s, %s, %s, 'stopped') RETURNING id",
            (name, script, command, enabled),
        )
        row = cur.fetchone()
    conn.commit()
    assert row is not None
    return int(row[0])


@pytest.fixture
def _point_backend_home(monkeypatch: pytest.MonkeyPatch, _pty_home: str) -> None:
    # One variable names the dedicated home for every resolver: the client dials the
    # service socket under it, so the in-process resolver and the service agree.
    monkeypatch.setitem(os.environ, "AVA_HOME", _pty_home)


@pytest.fixture
def _host_is_serving(serving_root: RootBirth, _point_backend_home: None) -> Iterator[None]:
    """PTY schedule cases model a gateway that completed its start gate."""
    from base.deploy.lifecycle import start_serving

    generation = start_serving.begin_start()
    assert start_serving.mark_serving(generation, runtime=serving_root.runtime) is True
    try:
        yield
    finally:
        start_serving.clear_serving()


def _dump_logs(home: Path) -> str:
    """Diagnosis aid for CI-only failures: every session transcript + the service log."""
    diag: list[str] = []
    for p in [*sorted((home / "logs").glob("*.out.log")), home / "pty-sessions-service.log"]:
        if p.exists():
            diag.append(f"--- {p.name} ---\n" + p.read_text(errors="replace")[-1200:])
    return "\n".join(diag)


def _wait_session_gone(name: str, timeout_s: float = 20.0) -> None:
    backend = get_shell_backend()
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline and backend.has_session(name):
        time.sleep(0.3)
    assert not backend.has_session(name), f"session {name} still live"


def test_launch_cwd_is_the_checkout_root() -> None:
    """Schedule sessions start in the checkout root, where `python -m
    services.wake.schedule_manager.runner` and the relative `.venv/bin/python` resolve."""
    assert sm.REPO_ROOT == REPO


def test_pty_launch_live_capture(
    db_conn: psycopg.Connection,
    pool: ConnectionPool,
    _pty_home: str,
) -> None:
    """_launch through the real PTY backend: the session is live in the service,
    _live_ids sees it, capture returns the runner's output."""
    # A real .py script (command must name the script file — a bare command
    # with no extension made the runner execute the script body as .py and
    # crash instantly; round 4 CI, 2893). The print marks real execution so
    # the assertion cannot false-pass on a traceback's module path.
    sid = _insert_schedule(
        db_conn,
        "pty-live",
        "import time; from ava.sdk_surface.agent_identity import require_actor; "
        "print('RUNNER_ALIVE', require_actor()); time.sleep(30)",
        "python schedule.py",
    )
    mgr = sm.ScheduleManager(pool)
    mgr.reconcile()

    name = session_name(f"schedule-{sid}")
    backend = get_shell_backend()
    assert backend.has_session(name)
    assert name in [info.name for info in client.list_sessions()], "service does not list it"
    assert sid in mgr._live_ids()
    # Poll for the runner's output instead of one immediate capture: under a
    # slow CI login shell the launch-to-output window is variable, and a
    # single probe can land before the command is delivered or after the
    # runner has already exited (2893). A session that dies mid-poll dumps
    # the session logs — the runner's traceback lands in the .out.log.
    home = Path(_pty_home)
    captured: str | None = None
    deadline = time.monotonic() + 60.0
    while time.monotonic() < deadline:
        try:
            captured = session_control.capture_blocking(sid, 50)
        except Exception as exc:
            raise AssertionError(
                f"capture failed ({exc}); session logs:\n{_dump_logs(home)}"
            ) from exc
        if captured and f"RUNNER_ALIVE schedule:{sid}" in captured:
            break
        time.sleep(0.5)
    if not (captured and f"RUNNER_ALIVE schedule:{sid}" in captured):
        raise AssertionError("runner output never appeared; session logs:\n" + _dump_logs(home))

    assert mgr._reap(sid)
    _wait_session_gone(name)


def test_pty_crash_is_not_replayed_after_manager_restart(
    db_conn: psycopg.Connection, pool: ConnectionPool, _pty_home: str
) -> None:
    """A real script effect occurs once; fresh managers never repeat it."""
    marker = Path(_pty_home) / "crash-effects"
    script = f"printf 'effect\\n' >> {marker}; exit 1"
    sid = _insert_schedule(db_conn, "pty-crash", script, "bash run.sh")
    mgr = sm.ScheduleManager(pool)
    mgr.reconcile()
    name = session_name(f"schedule-{sid}")
    backend = get_shell_backend()
    assert backend.has_session(name)

    # The runner exits nonzero immediately → session ends on its own.
    _wait_session_gone(name)

    for _ in range(3):
        sm.ScheduleManager(pool).reconcile()
        assert not backend.has_session(name)
    row = db_conn.execute(
        "SELECT status, launch_count FROM schedules WHERE id = %s", (sid,)
    ).fetchone()
    assert row == ("error", 1)
    assert marker.read_text().splitlines() == ["effect"]

    # An explicit restart is a new revision and deliberately authorizes a new effect.
    db_conn.execute(
        "UPDATE schedules SET desired_revision = desired_revision + 1, "
        "launch_count = 0, status = 'stopped' WHERE id = %s",
        (sid,),
    )
    db_conn.commit()
    assert sm.ScheduleManager(pool).sync_one(sid)
    _wait_session_gone(name)
    sm.ScheduleManager(pool).reconcile()
    assert marker.read_text().splitlines() == ["effect", "effect"]


def test_real_pty_revision_is_adopted_after_lost_acknowledgement(
    db_conn: psycopg.Connection,
    pool: ConnectionPool,
    _pty_home: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from services.wake.schedule_manager import revisions

    # A real PTY runner stub holds the exact pre-acknowledgement window open;
    # the ordinary runner would also write running during its own startup.
    marker = tmp_path / "runner-births"
    runner = tmp_path / ".venv" / "bin" / "python"
    runner.parent.mkdir(parents=True)
    runner.write_text(
        f"#!/bin/sh\nprintf 'birth\\n' >> {shlex.quote(str(marker))}\nexec sleep 30\n"
    )
    runner.chmod(0o755)
    monkeypatch.setattr(sm, "REPO_ROOT", tmp_path)
    sid = _insert_schedule(
        db_conn, "pty-revision", "import time; time.sleep(30)", "python schedule.py"
    )
    db_conn.execute("UPDATE schedules SET desired_revision = 1 WHERE id = %s", (sid,))
    db_conn.commit()
    name = session_name(f"schedule-{sid}")
    mgr = sm.ScheduleManager(pool)
    monkeypatch.setattr(mgr, "_mark_running", MagicMock(side_effect=RuntimeError("lost ack")))
    try:
        with pytest.raises(RuntimeError, match="lost ack"):
            mgr.sync_one(sid)
        assert revisions.launched_revision(sid) == 1, repr(client.live_sessions(name))
        before = get_shell_backend().session_started_at(name)
        assert before is not None
        assert wait_for(marker.exists)
        assert db_conn.execute(
            "SELECT status, launch_count FROM schedules WHERE id = %s", (sid,)
        ).fetchone() == ("error", 1)
        sm.ScheduleManager(pool).reconcile()
        assert get_shell_backend().session_started_at(name) == before
        assert db_conn.execute(
            "SELECT status, launch_count, last_error FROM schedules WHERE id = %s", (sid,)
        ).fetchone() == ("running", 1, None)
        assert marker.read_text().splitlines() == ["birth"]
    finally:
        mgr._reap(sid)
        _wait_session_gone(name)
