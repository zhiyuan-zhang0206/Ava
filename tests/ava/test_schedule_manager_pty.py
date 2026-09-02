"""ScheduleManager × real PTY supervisor integration (S6 schedule migration).

Real session hosts + real DB + real backend + the real `gateway.schedule_runner`
entrypoint: `_launch` creates a PTY-supervisor session, `_live_ids` /
`capture_blocking` see it, `_reap` tears it down, reconcile rebuilds after a
crash, and the breaker trips after repeated crashes.

The schedule commands are deliberately trivial (`sleep 30` for the live
paths, `false` for the crash paths) — the runner's real script execution and
status semantics are covered by test_schedule_runner.py.
"""

import os
import time
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from gateway import schedule_manager as sm
from shared.cluster import session_name
from shared.platform import IS_WINDOWS
from shared.session_backend import get_shell_backend

REPO = Path(__file__).resolve().parents[2]

pytestmark = [
    pytest.mark.skipif(IS_WINDOWS, reason="pty sessions are POSIX-only"),
    pytest.mark.usefixtures("_pty_home"),
]


@pytest.fixture(scope="module")
def _pty_home(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """A DEDICATED home for the schedule sessions' records/sockets/hosts.

    There is no daemon to spawn: each schedule launch spawns its own detached
    session host under this home (the CLI children inherit AVA_HOME from the
    pinned env). Torn down by killing whatever sessions are still alive under
    the home — hosts detach to init, so an unkilled one would outlive the
    test run."""
    import os

    home = tmp_path_factory.mktemp("pty-sched-home")
    # The schedule runner builds the full gateway settings from the home's
    # .env; point it at the test database so a `sleep 30` schedule stays up
    # long enough to observe (the runner's own semantics are covered by
    # test_schedule_runner.py).
    from shared.config import settings as _settings

    (home / ".env").write_text(f"AVA_DB_URL={_settings.data_plane.db_url}\n")
    prior_home = os.environ.get("AVA_HOME")
    prior_override = os.environ.get("AVA_HOME_OVERRIDE")
    os.environ["AVA_HOME"] = str(home)
    os.environ["AVA_HOME_OVERRIDE"] = "1"
    try:
        yield str(home)
    finally:
        from shared.pty_sessions import cli as pty_cli

        for name in list(pty_cli.live_sessions()):
            try:
                pty_cli.session_request(name, {"op": "kill"})
            except OSError:
                pty_cli._kill_by_record(name)
        for key, prior in (("AVA_HOME", prior_home), ("AVA_HOME_OVERRIDE", prior_override)):
            if prior is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = prior


@pytest.fixture(scope="module")
def pool() -> Iterator[ConnectionPool[psycopg.Connection]]:
    from shared.config import settings

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
    # Point EVERY resolver at the dedicated home: the PtySessionBackend CLI
    # children inherit os.environ (setitem, not setenv — the env is for
    # subprocess children, not the settings singleton), and the backend's
    # list/started-at enumeration resolves the record dir in-process from
    # settings.general.ava_home — pin that too so the in-process resolver and
    # the spawned hosts agree (they always agree in production, where one
    # process carries one home).
    monkeypatch.setitem(os.environ, "AVA_HOME", _pty_home)
    monkeypatch.setitem(os.environ, "AVA_HOME_OVERRIDE", "1")
    from shared.config import settings

    monkeypatch.setattr(settings.general, "ava_home", Path(_pty_home))


def _dump_logs(home: Path) -> str:
    """Diagnosis aid for CI-only failures: every session transcript + host log."""
    diag: list[str] = []
    logdir = home / "logs"
    for p in sorted(logdir.glob("*.out.log")) + sorted(logdir.glob("*.host.log")):
        diag.append(f"--- {p.name} ---\n" + p.read_text()[-1200:])
    return "\n".join(diag)


def _wait_session_gone(name: str, timeout_s: float = 20.0) -> None:
    backend = get_shell_backend()
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline and backend.has_session(name):
        time.sleep(0.3)
    assert not backend.has_session(name), f"session {name} still live"


def test_pty_launch_live_capture(
    db_conn: psycopg.Connection,
    pool: ConnectionPool,
    _pty_home: str,
) -> None:
    """_launch through the real PTY backend: session appears in the record
    namespace, _live_ids sees it, capture returns the runner's output."""
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
    mgr._reconcile()

    name = session_name(f"schedule-{sid}")
    backend = get_shell_backend()
    assert backend.has_session(name)
    assert (Path(_pty_home) / "run" / "pty" / f"{name}.json").exists(), "record file missing"
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
            captured = mgr._capture_blocking(sid, 50)
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
    mgr._reconcile()
    name = session_name(f"schedule-{sid}")
    backend = get_shell_backend()
    assert backend.has_session(name)
    _sm = sm

    # The runner exits nonzero immediately → session ends on its own.
    _wait_session_gone(name)

    clock = {"t": time.monotonic()}
    orig = _sm.time.monotonic
    _sm.time.monotonic = lambda: clock["t"]  # type: ignore[method-assign]
    try:
        clock["t"] += _sm._BACKOFF_CAP_S + 1  # clear the first launch's backoff
        mgr._reconcile()  # relaunch
        assert backend.has_session(name)
    finally:
        _sm.time.monotonic = orig  # type: ignore[method-assign]
    assert mgr._reap(sid)
    _wait_session_gone(name)


def test_pty_breaker_trips_after_repeated_crashes(
    db_conn: psycopg.Connection, pool: ConnectionPool
) -> None:
    """The circuit breaker still trips on the PTY backend: after _BREAKER_MAX
    crash/relaunch rounds the schedule lands in status='error' and is left
    alone."""
    sid = _insert_schedule(db_conn, "pty-breaker", "exit 1", "bash run.sh")
    clock = {"t": 0.0}
    import gateway.schedule_manager as _sm

    orig_monotonic = _sm.time.monotonic
    _sm.time.monotonic = lambda: clock["t"]  # type: ignore[method-assign]
    try:
        mgr = sm.ScheduleManager(pool)
        name = session_name(f"schedule-{sid}")
        backend = get_shell_backend()
        for _ in range(_sm._BREAKER_MAX + 3):
            mgr._reconcile()
            if backend.has_session(name):
                _wait_session_gone(name)
            clock["t"] += _sm._BACKOFF_CAP_S + 1
        with db_conn.cursor() as cur:
            cur.execute("SELECT status FROM schedules WHERE id = %s", (sid,))
            row = cur.fetchone()
        assert row is not None and row[0] == "error", f"breaker did not trip: {row}"
        # A tripped schedule is terminal: reconcile must not relaunch it.
        before = backend.has_session(name)
        mgr._reconcile()
        assert not backend.has_session(name) or before
    finally:
        _sm.time.monotonic = orig_monotonic  # type: ignore[method-assign]
