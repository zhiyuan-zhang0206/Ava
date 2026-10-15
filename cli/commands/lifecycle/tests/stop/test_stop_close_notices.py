from __future__ import annotations

import shlex
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import psutil
import psycopg
import pytest

from base.db import create_agent
from base.native_process.ownership import OwnedProcess
from base.sessions.pty import closure
from base.sessions.pty.paths import ledger_path
from base.sessions.pty.tests import pty_jobs as jobs
from base.sessions.pty.tests import pty_shells
from base.sessions.pty.tests.pty_reaper import PtyReaper
from base.sessions.pty.tests.pty_reaper import pty_reaper as pty_reaper
from base.sessions.tests.refuse_helper import refuse_helper
from base.telemetry import EventPipeline
from cli.commands.lifecycle import _temporary_stop as command
from cli.commands.lifecycle import stop as entry
from cli.commands.lifecycle.tests.stop.stop_support import (
    WRITE_NOTICES,
    PtyServiceProcess,
    busy_session,
    dependencies,
    stop_env,
)
from cli.commands.lifecycle.tests.stop.stop_support import home as home
from cli.commands.lifecycle.tests.stop.stop_support import pty_service as pty_service
from cli.commands.lifecycle.tests.stop.stop_support import written as written
from cli.database.runtime import CommandRuntime
from cli.database.tests.command_inputs import command_runtime as command_runtime
from ops import pty_close_notices
from services.agent_runner.pty_sessions import ledger
from tests.path_scoped.cli_tests import operator_database as operator_database
from tests.path_scoped.cli_tests import operator_pipeline as operator_pipeline


def test_busy_fixture_waits_for_the_job_after_a_login_helper(
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    pty_reaper: PtyReaper,
    pty_service: PtyServiceProcess,
) -> None:
    name = "ava-agent-987-shell-2044-login-readiness"
    release = home / "release-login-helper"
    startup = home / "login-helper.py"
    startup.write_text(
        "import time\nfrom pathlib import Path\n"
        "print('login-helper-ready', flush=True)\n"
        f"while not Path({str(release)!r}).exists(): time.sleep(0.01)\n",
        encoding="utf-8",
    )
    (home / ".bash_profile").write_text(
        f"{shlex.quote(sys.executable)} -u {shlex.quote(str(startup))}\n", encoding="utf-8"
    )
    original = pty_shells.screen

    def screen(target: str, *, scrollback: bool = True) -> str:
        output = original(target, scrollback=scrollback)
        if target == name and "login-helper-ready" in output.splitlines():
            release.touch()
        return output

    monkeypatch.setattr(pty_shells, "screen", screen)
    try:
        busy_session(home, name, jobs.TERM_OK, pty_reaper)
        assert "job-ready" in pty_shells.screen(name).splitlines()
    finally:
        release.touch()


def test_stop_records_notice_for_verified_closed_busy_session(
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    pty_reaper: PtyReaper,
    pty_service: PtyServiceProcess,
    written: list[pty_close_notices.ClosureNotice],
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
) -> None:
    dependencies(monkeypatch, machine=command_runtime.machine)
    stop_env(monkeypatch, home)
    name = "ava-agent-987-shell-2044-busy"
    busy_session(home, name, jobs.TERM_OK, pty_reaper)

    assert (
        entry.cmd_stop(
            require_confirmation=False,
            keep_infra=True,
            timeout=15,
            database_factory=operator_database,
            producer=operator_pipeline,
            machine=command_runtime.machine,
            helper=refuse_helper,
        )
        == 0
    )
    assert len(written) == 1
    notice = written[0]
    assert notice.agent_id == 987
    assert notice.session_id == 2044
    assert notice.name == name
    assert notice.machine
    assert notice.operation
    assert "operator stop" in notice.reason


@pytest.mark.parametrize(
    ("roles", "direct", "stops_data_plane"),
    [
        (frozenset({"agent-runner"}), False, False),
        (frozenset({"gateway", "agent-runner"}), True, True),
    ],
)
def test_notices_are_written_once_in_the_terminals_phase_before_the_data_plane_stops(
    roles: frozenset[str],
    direct: bool,
    stops_data_plane: bool,
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    pty_reaper: PtyReaper,
    pty_service: PtyServiceProcess,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
) -> None:
    dependencies(monkeypatch, machine=command_runtime.machine)
    stop_env(monkeypatch, home)
    command_runtime.machine.set(role=roles)
    events: list[str] = []

    def write(
        _db: object, _bus: object, batch: Sequence[pty_close_notices.ClosureNotice], *, direct: bool
    ) -> list[tuple[pty_close_notices.ClosureNotice, Exception]]:
        events.append(f"write:{len(batch)}:direct={direct}")
        return []

    def stop_data_plane(_timeout: float, **_kw: object) -> list[str]:
        events.append("data-plane")
        return []

    monkeypatch.setattr(pty_close_notices, "write_notices", write)
    monkeypatch.setattr(command, "stop_data_plane", stop_data_plane)
    name = "ava-agent-987-shell-2044-order"
    busy_session(home, name, jobs.TERM_OK, pty_reaper)

    assert (
        entry.cmd_stop(
            require_confirmation=False,
            keep_infra=False,
            timeout=15,
            database_factory=operator_database,
            producer=operator_pipeline,
            machine=command_runtime.machine,
            helper=refuse_helper,
        )
        == 0
    )
    assert events == [f"write:1:direct={direct}", *(["data-plane"] if stops_data_plane else [])]


def test_the_stop_writes_the_owners_inbound_to_the_database(
    redis_url: str,
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    pty_reaper: PtyReaper,
    pty_service: PtyServiceProcess,
    db_conn: psycopg.Connection,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
) -> None:
    dependencies(monkeypatch, machine=command_runtime.machine)
    stop_env(monkeypatch, home)
    monkeypatch.setattr(pty_close_notices, "write_notices", WRITE_NOTICES)
    owner = create_agent(db_conn)
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agents_meta (id, spawner, status) VALUES (%s, 'user', 'running')",
            (owner,),
        )
    db_conn.commit()
    name = f"ava-agent-{owner}-shell-2044-landed"
    busy_session(home, name, jobs.TERM_OK, pty_reaper)

    assert (
        entry.cmd_stop(
            require_confirmation=False,
            keep_infra=True,
            timeout=15,
            database_factory=operator_database,
            producer=operator_pipeline,
            machine=command_runtime.machine,
            helper=refuse_helper,
        )
        == 0
    )
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT source, content, payload->'closure'->>'name' FROM inbound_messages "
            "WHERE agent_id = %s",
            (owner,),
        )
        rows = cur.fetchall()
    assert len(rows) == 1
    source, content, closed_name = rows[0]
    assert source == "system" and closed_name == name
    assert "closed by an operator stop (ava stop)" in content


def test_an_unwritable_notice_is_loud_and_does_not_fail_the_stop(
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    pty_reaper: PtyReaper,
    pty_service: PtyServiceProcess,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
) -> None:
    dependencies(monkeypatch, machine=command_runtime.machine)
    stop_env(monkeypatch, home)

    def unreachable(
        _db: object, _bus: object, batch: Sequence[pty_close_notices.ClosureNotice], *, direct: bool
    ) -> list[tuple[pty_close_notices.ClosureNotice, Exception]]:
        del direct
        return [(notice, ConnectionError("connection refused")) for notice in batch]

    monkeypatch.setattr(pty_close_notices, "write_notices", unreachable)
    name = "ava-agent-987-shell-2044-unreachable"
    busy_session(home, name, jobs.TERM_OK, pty_reaper)

    assert (
        entry.cmd_stop(
            require_confirmation=False,
            keep_infra=True,
            timeout=15,
            database_factory=operator_database,
            producer=operator_pipeline,
            machine=command_runtime.machine,
            helper=refuse_helper,
        )
        == 0
    )
    err = capsys.readouterr().err
    assert name in err and "agent 987" in err and "connection refused" in err


def test_a_stop_without_the_service_tells_owners_the_service_crashed_not_that_it_stopped(
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    written: list[pty_close_notices.ClosureNotice],
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
) -> None:
    dependencies(monkeypatch, machine=command_runtime.machine)
    stop_env(monkeypatch, home)
    child = subprocess.Popen(["true"])
    gone = OwnedProcess.capture(psutil.Process(child.pid))
    child.wait(timeout=10)
    name = "ava-agent-987-shell-2044-crashed"
    ledger.write(
        ledger_path(), [closure.Target(name, gone, (gone, OwnedProcess(gone.pid, 0.0, None)))]
    )

    assert (
        entry.cmd_stop(
            require_confirmation=False,
            keep_infra=True,
            timeout=15,
            database_factory=operator_database,
            producer=operator_pipeline,
            machine=command_runtime.machine,
            helper=refuse_helper,
        )
        == 0
    )

    assert [(notice.name, notice.reason) for notice in written] == [
        (name, pty_close_notices.CRASH_REASON)
    ]
    assert ledger.read(ledger_path()) == []
