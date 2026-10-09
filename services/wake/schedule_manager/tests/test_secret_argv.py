"""Schedule launcher keeps secrets off real PTY process argv and runner environments."""

from __future__ import annotations

from pathlib import Path

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from base.native_process.os_platform import is_windows
from tests.factories.secret_argv import (
    SECRET_VALUES,
    assert_service_tree_clean,
    service_tree_argvs,
)
from tests.factories.secret_argv import (
    secrets_in_creator_env as secrets_in_creator_env,
)
from tests.path_scoped.pty_service import PtyServiceProcess
from tests.path_scoped.pty_service import pty_service as pty_service
from tests.path_scoped.pty_shells import wait_for

pytestmark = pytest.mark.skipif(
    is_windows(), reason="POSIX launch paths only (Windows hands env to CreateProcess)"
)


def test_schedule_launch(
    pty_service: PtyServiceProcess,
    secrets_in_creator_env: None,
    db_conn: psycopg.Connection,
    unit_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A schedule's resident process. The launch is a `new` request to the pty-sessions
    service: the env (the schedule id included) rides the request body of its unix
    socket, the command is typed into the session's shell, and no argv of the service
    or the shell tree carries a secret; the shell's environment holds the schedule
    id and none of the secrets."""
    from services.wake.schedule_manager import manager as sm
    from services.wake.schedule_manager.manager import ScheduleManager

    # The runner is a stub that records its own environment, then stays up: the real
    # runner would need a database and exit within the test.
    stub = unit_home / ".venv" / "bin" / "python"
    stub.parent.mkdir(parents=True)
    dump = unit_home / "runner-env.txt"
    stub.write_text(f"#!/bin/sh\nenv > {dump}.tmp && mv {dump}.tmp {dump}\nexec sleep 300\n")
    stub.chmod(0o755)
    monkeypatch.setattr(sm, "REPO_ROOT", unit_home)

    row = db_conn.execute(
        "INSERT INTO schedules (name, script, command, enabled, desired_revision) "
        "VALUES ('argv-security-probe', 'pass', 'python schedule.py', true, 1) RETURNING id"
    ).fetchone()
    assert row is not None
    schedule_id = int(row[0])
    db_conn.commit()
    # Launch reads the authoritative desired revision before creating a session.
    # Keep those reads/writes real while the runner stays a PTY stub.
    pool: ConnectionPool[psycopg.Connection] = ConnectionPool(
        db_conn.info.dsn, min_size=1, max_size=2, open=True
    )
    try:
        ScheduleManager(pool)._launch(schedule_id)
    finally:
        pool.close()

    assert wait_for(dump.exists), f"the runner never started:\n{pty_service.output()}"
    runner_env = dump.read_text()
    assert f"AVA_SCHEDULE_ID={schedule_id}\n" in runner_env, "the schedule id rides the request env"
    for secret in SECRET_VALUES:
        assert secret not in runner_env, f"{secret!r} reached the schedule's environment"
    assert_service_tree_clean(pty_service, label="ScheduleManager._launch")
    for argv in service_tree_argvs(pty_service):
        assert "AVA_SCHEDULE_ID" not in " ".join(argv), f"schedule id on argv: {argv!r}"
