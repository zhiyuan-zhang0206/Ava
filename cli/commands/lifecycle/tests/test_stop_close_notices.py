"""Shell-closure notices written by the stop's `terminals` phase (issue #2044).

Real persistent shells in a real pty-sessions service, closed by `ava stop`: which
notices are built, where in the stop they are written (before the data plane stops,
over one dial that depends on the unit's roles), and what a failed write does to
the stop.
"""

from __future__ import annotations

import shlex
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

import psutil
import psycopg
import pytest

from base.db import create_agent
from base.native_process.ownership import OwnedProcess
from base.sessions.pty import closure
from base.sessions.pty.paths import ledger_path
from cli.commands.lifecycle import _temporary_stop as command
from cli.commands.lifecycle import stop as entry
from cli.commands.lifecycle.tests.stop_support import (
    WRITE_NOTICES,
    PtyServiceProcess,
    busy_session,
    dependencies,
    stop_env,
)
from cli.commands.lifecycle.tests.stop_support import home as home
from cli.commands.lifecycle.tests.stop_support import pty_service as pty_service
from cli.commands.lifecycle.tests.stop_support import written as written
from ops import pty_close_notices
from services.agent_runner.pty_sessions import ledger
from tests.path_scoped import pty_jobs as jobs
from tests.path_scoped import pty_shells
from tests.path_scoped.pty_reaper import PtyReaper
from tests.path_scoped.pty_reaper import pty_reaper as pty_reaper


def test_busy_fixture_waits_for_the_job_after_a_login_helper(
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    pty_reaper: PtyReaper,
    pty_service: PtyServiceProcess,
) -> None:
    """A login-profile child is not evidence that the requested job started."""
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

    # Release the native profile gate on its first real captured output. The
    # old child-existence helper returns before this and sees no job marker.
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
) -> None:
    """A busy session verified closed gets one notice, written by the stop
    itself (issue #2044)."""
    dependencies(monkeypatch)
    stop_env(monkeypatch, home)
    name = "ava-agent-987-shell-2044-busy"
    busy_session(home, name, jobs.TERM_OK, pty_reaper)

    assert entry.cmd_stop(require_confirmation=False, keep_infra=True, timeout=15) == 0
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
) -> None:
    """The write is one call inside the `terminals` phase, with the database
    still up: a gateway unit dials its own Postgres directly, a runner-only
    unit (no local data plane to stop) dials its configured URL. Nothing is
    written once the data-plane phase has begun."""
    dependencies(monkeypatch)
    stop_env(monkeypatch, home)
    monkeypatch.setattr(command, "machine_role", lambda: roles)
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

    assert entry.cmd_stop(require_confirmation=False, keep_infra=False, timeout=15) == 0
    assert events == [f"write:1:direct={direct}", *(["data-plane"] if stops_data_plane else [])]


def test_the_stop_writes_the_owners_inbound_to_the_database(
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    pty_reaper: PtyReaper,
    pty_service: PtyServiceProcess,
    db_conn: psycopg.Connection,
) -> None:
    """End to end through the real write: the owner of the closed busy session
    has one pending system inbound naming the closure when the stop returns."""
    dependencies(monkeypatch)
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

    assert entry.cmd_stop(require_confirmation=False, keep_infra=True, timeout=15) == 0
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
) -> None:
    """The database became unreachable after the stop began: the notice is not
    dropped quietly — stderr names its agent and session — and the stop, whose
    terminals are closed either way, still succeeds."""
    dependencies(monkeypatch)
    stop_env(monkeypatch, home)

    def unreachable(
        _db: object, _bus: object, batch: Sequence[pty_close_notices.ClosureNotice], *, direct: bool
    ) -> list[tuple[pty_close_notices.ClosureNotice, Exception]]:
        del direct
        return [(notice, ConnectionError("connection refused")) for notice in batch]

    monkeypatch.setattr(pty_close_notices, "write_notices", unreachable)
    name = "ava-agent-987-shell-2044-unreachable"
    busy_session(home, name, jobs.TERM_OK, pty_reaper)

    assert entry.cmd_stop(require_confirmation=False, keep_infra=True, timeout=15) == 0
    err = capsys.readouterr().err
    assert name in err and "agent 987" in err and "connection refused" in err


def test_a_stop_without_the_service_tells_owners_the_service_crashed_not_that_it_stopped(
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    written: list[pty_close_notices.ClosureNotice],
) -> None:
    """No service listens: what its ledger last saw running was lost to the service dying,
    before this stop, so the owner is told that — even when the crash had already ended
    every process of the session — and the stop's own hold is not blamed."""
    dependencies(monkeypatch)
    stop_env(monkeypatch, home)
    child = subprocess.Popen(["true"])
    gone = OwnedProcess.capture(psutil.Process(child.pid))
    child.wait(timeout=10)
    name = "ava-agent-987-shell-2044-crashed"
    ledger.write(
        ledger_path(), [closure.Target(name, gone, (gone, OwnedProcess(gone.pid, 0.0, None)))]
    )

    assert entry.cmd_stop(require_confirmation=False, keep_infra=True, timeout=15) == 0

    assert [(notice.name, notice.reason) for notice in written] == [
        (name, pty_close_notices.CRASH_REASON)
    ]
    assert ledger.read(ledger_path()) == []
