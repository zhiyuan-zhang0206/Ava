"""ScheduleManager x real pty-sessions service integration.

A real pty-sessions service + real DB + real backend + the real
`gateway.schedules.runner` entrypoint: `_launch` creates a session in the service,
`_live_ids` / `capture_blocking` see it, `_reap` tears it down, reconcile rebuilds
after a crash, and the breaker trips after repeated crashes.

The schedule commands are deliberately trivial (`sleep 30` for the live
paths, `false` for the crash paths) — the runner's real script execution and
status semantics are covered by test_schedule_runner.py.
"""

import contextlib
import os
import time
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from base.cluster import session_name
from base.deploy.lifecycle.start_serving import RootBirth
from base.native_process.os_platform import is_windows
from base.sessions.backend import get_shell_backend
from base.sessions.pty import client
from gateway.schedules import session_control
from services.wake.schedule_manager import manager as sm
from tests.path_scoped.pty_service import PtyServiceProcess

REPO = Path(__file__).resolve().parents[4]

pytestmark = [
    pytest.mark.skipif(is_windows(), reason="pty sessions are POSIX-only"),
    pytest.mark.usefixtures("_pty_home"),
]


@pytest.fixture(scope="module")
def _pty_home(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """A DEDICATED home, with its own pty-sessions service, for the schedule sessions.

    The service is a subprocess pinned to this home; each schedule launch creates a
    session in it. Torn down by closing every session and stopping the service, so
    no shell outlives the test run."""
    import os

    home = tmp_path_factory.mktemp("pty-sched-home")
    # The schedule runner builds the full gateway settings from the home's
    # .env; point it at the test database so a `sleep 30` schedule stays up
    # long enough to observe (the runner's own semantics are covered by
    # test_schedule_runner.py).
    from base.config import settings as _settings

    (home / ".env").write_text(f"AVA_DB_URL={_settings.data_plane.db_url}\n")
    prior_home = os.environ.get("AVA_HOME")
    os.environ["AVA_HOME"] = str(home)
    service = PtyServiceProcess(home)
    try:
        service.start()
        yield str(home)
    finally:
        with contextlib.suppress(client.ServiceUnavailableError, client.ServiceError):
            client.close_all(grace_s=0.5, kill_s=2.0)
        service.stop()
        if prior_home is None:
            os.environ.pop("AVA_HOME", None)
        else:
            os.environ["AVA_HOME"] = prior_home


@pytest.fixture(scope="module")
def pool() -> Iterator[ConnectionPool[psycopg.Connection]]:
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


@pytest.fixture(autouse=True)
def _point_backend_home(monkeypatch: pytest.MonkeyPatch, _pty_home: str) -> None:
    # One variable names the dedicated home for every resolver: the client dials the
    # service socket under it, so the in-process resolver and the service agree.
    monkeypatch.setitem(os.environ, "AVA_HOME", _pty_home)


@pytest.fixture(autouse=True)
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
    gateway.schedules.runner` and the relative `.venv/bin/python` resolve."""
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
        "import time; print('RUNNER_ALIVE'); time.sleep(30)",
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
        if captured and "RUNNER_ALIVE" in captured:
            break
        time.sleep(0.5)
    if not (captured and "RUNNER_ALIVE" in captured):
        raise AssertionError("runner output never appeared; session logs:\n" + _dump_logs(home))

    assert mgr._reap(sid)
    _wait_session_gone(name)


def test_pty_crash_then_reconcile_rebuilds(
    db_conn: psycopg.Connection, pool: ConnectionPool, _pty_home: str
) -> None:
    """A crashing runner (a `bash run.sh` that exits 1) dies on its own — pty
    EOF closes the session — and the next reconcile relaunches it."""
    sid = _insert_schedule(db_conn, "pty-crash", "exit 1", "bash run.sh")
    mgr = sm.ScheduleManager(pool)
    mgr.reconcile()
    name = session_name(f"schedule-{sid}")
    backend = get_shell_backend()
    assert backend.has_session(name)

    # The runner exits nonzero immediately → session ends on its own.
    _wait_session_gone(name)

    # Clear the first launch's backoff window.
    db_conn.execute(
        "UPDATE schedules SET next_launch_at = clock_timestamp() - interval '1 second' "
        "WHERE id = %s",
        (sid,),
    )
    db_conn.commit()
    mgr.reconcile()  # relaunch
    assert backend.has_session(name)
    assert mgr._reap(sid)
    _wait_session_gone(name)


def test_pty_breaker_trips_after_repeated_crashes(
    db_conn: psycopg.Connection, pool: ConnectionPool
) -> None:
    """The circuit breaker still trips on the PTY backend: after _BREAKER_MAX
    crash/relaunch rounds the schedule lands in status='error' and is left
    alone."""
    sid = _insert_schedule(db_conn, "pty-breaker", "exit 1", "bash run.sh")
    mgr = sm.ScheduleManager(pool)
    name = session_name(f"schedule-{sid}")
    backend = get_shell_backend()
    for _ in range(sm._BREAKER_MAX + 3):
        mgr.reconcile()
        if backend.has_session(name):
            _wait_session_gone(name)
        db_conn.execute(
            "UPDATE schedules SET next_launch_at = clock_timestamp() - interval '1 second' "
            "WHERE id = %s",
            (sid,),
        )
        db_conn.commit()
    with db_conn.cursor() as cur:
        cur.execute("SELECT status FROM schedules WHERE id = %s", (sid,))
        row = cur.fetchone()
    assert row is not None and row[0] == "error", f"breaker did not trip: {row}"
    # A tripped schedule is terminal: reconcile must not relaunch it.
    before = backend.has_session(name)
    mgr.reconcile()
    assert not backend.has_session(name) or before


def test_real_pty_revision_is_adopted_after_lost_acknowledgement(
    db_conn: psycopg.Connection, pool: ConnectionPool, _pty_home: str
) -> None:
    from services.wake.schedule_manager import revisions

    sid = _insert_schedule(
        db_conn, "pty-revision", "import time; time.sleep(30)", "python schedule.py"
    )
    db_conn.execute("UPDATE schedules SET desired_revision = 1 WHERE id = %s", (sid,))
    db_conn.commit()
    name = session_name(f"schedule-{sid}")
    mgr = sm.ScheduleManager(pool)
    try:
        assert mgr.sync_one(sid)
        assert revisions.launched_revision(sid) == 1, repr(client.live_sessions(name))
        before = get_shell_backend().session_started_at(name)
        assert before is not None
        assert sm.ScheduleManager(pool).sync_one(sid)
        assert get_shell_backend().session_started_at(name) == before
    finally:
        mgr._reap(sid)
        _wait_session_gone(name)
