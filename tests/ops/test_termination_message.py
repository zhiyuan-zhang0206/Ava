"""Atomic persistence and failure-priority tests for termination messages."""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import psycopg
import pytest
from psycopg_pool import ConnectionPool
from pydantic import ValidationError

from ops import ops_exit
from ops.rpc_schemas import TerminateAgentRequest
from shared.config import settings
from shared.db import create_agent
from shared.telemetry import Event


@pytest.fixture
def running_agent_id(db_conn: psycopg.Connection) -> int:
    agent_id = create_agent(db_conn)
    db_conn.execute(
        "INSERT INTO agents_meta (id,status,machine) VALUES (%s,'running','test-machine')",
        (agent_id,),
    )
    db_conn.commit()
    return agent_id


@pytest.fixture
def db_pool() -> Iterator[ConnectionPool]:
    with ConnectionPool(
        settings.data_plane.db_url,
        min_size=1,
        max_size=1,
        kwargs={"prepare_threshold": None},
    ) as pool:
        yield cast(ConnectionPool, pool)


class TestTerminateAgentRequestMessage:
    def test_normalizes_non_empty_message(self) -> None:
        body = TerminateAgentRequest(message="  leave the findings in the log  ")
        assert body.message == "leave the findings in the log"

    @pytest.mark.parametrize("message", ["", "   ", "x" * 1_000_001])
    def test_rejects_invalid_message(self, message: str) -> None:
        with pytest.raises(ValidationError):
            TerminateAgentRequest(message=message)

    def test_message_requires_a_chat_source(self) -> None:
        with pytest.raises(ValidationError, match="Unrecognized inbound source"):
            TerminateAgentRequest(message="final note", source="machine-pause")


def test_termination_inbounds_are_atomic_and_fall_back_to_pending_message(
    db_conn: psycopg.Connection,
    db_pool: ConnectionPool,
    running_agent_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed pair rolls back both rows, then retries terminate before chat."""
    real_insert = ops_exit._insert_termination_pair
    failed_pair = False

    def _fail_after_pair(
        conn: psycopg.Connection,
        agent_id: int,
        *,
        source: str,
        message: str | None,
        kill_all_shell_sessions: bool = False,
    ) -> tuple[int | None, int]:
        nonlocal failed_pair
        result = real_insert(
            conn,
            agent_id,
            source=source,
            message=message,
            kill_all_shell_sessions=kill_all_shell_sessions,
        )
        if message is not None and not failed_pair:
            failed_pair = True
            raise RuntimeError("injected pair failure")
        return result

    monkeypatch.setattr(ops_exit, "_insert_termination_pair", _fail_after_pair)
    terminate_id = ops_exit._enqueue_termination_inbounds(
        running_agent_id,
        db_pool,
        source="user",
        message="final note",
    )

    rows = db_conn.execute(
        "SELECT id,content,kind,source,status FROM inbound_messages WHERE agent_id=%s ORDER BY id",
        (running_agent_id,),
    ).fetchall()
    assert failed_pair
    assert rows == [
        (terminate_id, "", "terminate", "user", "pending"),
        (rows[1][0], "final note", "chat", "user", "pending"),
    ]


def test_termination_survives_failed_message_retry(
    db_conn: psycopg.Connection,
    db_pool: ConnectionPool,
    running_agent_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Once fallback terminate succeeds, a second chat failure is non-fatal."""
    real_insert = ops_exit._insert_termination_pair

    def _fail_pair(
        conn: psycopg.Connection,
        agent_id: int,
        *,
        source: str,
        message: str | None,
        kill_all_shell_sessions: bool = False,
    ) -> tuple[int | None, int]:
        if message is not None:
            raise RuntimeError("injected pair failure")
        return real_insert(
            conn,
            agent_id,
            source=source,
            message=message,
            kill_all_shell_sessions=kill_all_shell_sessions,
        )

    def _fail_retry(*_args: object, **_kwargs: object) -> int:
        raise RuntimeError("injected retry failure")

    monkeypatch.setattr(ops_exit, "_insert_termination_pair", _fail_pair)
    monkeypatch.setattr(ops_exit, "_insert_pending_termination_message", _fail_retry)
    terminate_id = ops_exit._enqueue_termination_inbounds(
        running_agent_id,
        db_pool,
        source="user",
        message="final note",
    )

    assert db_conn.execute(
        "SELECT id,content,kind,status FROM inbound_messages WHERE agent_id=%s",
        (running_agent_id,),
    ).fetchall() == [(terminate_id, "", "terminate", "pending")]


def test_force_termination_retries_command_before_message(
    db_conn: psycopg.Connection,
    db_pool: ConnectionPool,
    running_agent_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Force termination uses the same rollback and termination-first fallback."""
    real_insert = ops_exit._insert_termination_pair
    failed_pair = False

    def _fail_after_pair(
        conn: psycopg.Connection,
        agent_id: int,
        *,
        source: str,
        message: str | None,
        kill_all_shell_sessions: bool = False,
    ) -> tuple[int | None, int]:
        nonlocal failed_pair
        result = real_insert(
            conn,
            agent_id,
            source=source,
            message=message,
            kill_all_shell_sessions=kill_all_shell_sessions,
        )
        if message is not None and not failed_pair:
            failed_pair = True
            raise RuntimeError("injected force pair failure")
        return result

    monkeypatch.setattr(ops_exit, "_insert_termination_pair", _fail_after_pair)
    old_status, _, _, terminate_id = ops_exit._force_terminate_transaction(
        running_agent_id,
        db_pool,
        source="user",
        message="final force note",
    )

    rows = db_conn.execute(
        "SELECT id,content,kind,status FROM inbound_messages WHERE agent_id=%s ORDER BY id",
        (running_agent_id,),
    ).fetchall()
    assert failed_pair
    assert old_status.value == "running"
    assert rows == [
        (terminate_id, "", "terminate", "pending"),
        (rows[1][0], "final force note", "chat", "pending"),
    ]
    assert db_conn.execute(
        "SELECT status,last_force_terminate_inbound_id FROM agents_meta WHERE id=%s",
        (running_agent_id,),
    ).fetchone() == ("terminated", terminate_id)


def _terminate_payloads(db_conn: psycopg.Connection, agent_id: int) -> list[object]:
    return [
        row[0]
        for row in db_conn.execute(
            "SELECT payload FROM inbound_messages WHERE agent_id=%s AND kind='terminate' "
            "ORDER BY id",
            (agent_id,),
        ).fetchall()
    ]


class TestKillAllShellSessions:
    """`kill_all_shell_sessions` on terminate / kill
    (decisions/2026-09-27-terminate-has-no-closed-state.md): a graceful
    terminate of a live agent records the request on its terminate command and
    kills nothing yet; a force terminate, or an agent that is already
    terminated, has its sessions killed before the response, which reports
    them. Without the option nothing is recorded and nothing is killed."""

    @pytest.fixture
    def kills(self, monkeypatch: pytest.MonkeyPatch) -> list[int]:
        from ops import ops_lifecycle

        calls: list[int] = []

        def _kill(agent_id: int) -> list[int]:
            calls.append(agent_id)
            return [0, 3]

        async def _noop_cancel(_aid: int, _command_id: int) -> None:
            return None

        monkeypatch.setattr(ops_lifecycle, "kill_agent_shells", _kill)
        monkeypatch.setattr(ops_lifecycle, "_cancel_hosted_turn_best_effort", _noop_cancel)
        return calls

    @pytest.mark.asyncio
    async def test_graceful_live_agent_defers_the_kill_to_exit(
        self,
        db_conn: psycopg.Connection,
        db_pool: ConnectionPool,
        running_agent_id: int,
        kills: list[int],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from ops import ops_lifecycle
        from ops.rpc_terminate import ShellSessionsKill

        events: list[Event] = []
        monkeypatch.setattr(ops_exit.telemetry, "emit_prepared", events.append)

        resp = await ops_lifecycle.terminate_agent_op(
            running_agent_id, TerminateAgentRequest(kill_all_shell_sessions=True), db_pool
        )

        assert resp.status == "enqueued"
        assert resp.shell_sessions == ShellSessionsKill(when="at_exit", killed=[])
        assert kills == []  # the last step still runs; its sessions stay up until exit
        assert _terminate_payloads(db_conn, running_agent_id) == [{"kill_all_shell_sessions": True}]
        assert [event.attributes for event in events] == [
            {"inbound_id": events[0].attributes["inbound_id"], "kill_all_shell_sessions": True}
        ]

    @pytest.mark.asyncio
    async def test_without_the_option_nothing_is_recorded_or_killed(
        self,
        db_conn: psycopg.Connection,
        db_pool: ConnectionPool,
        running_agent_id: int,
        kills: list[int],
    ) -> None:
        from ops import ops_lifecycle

        graceful = await ops_lifecycle.terminate_agent_op(
            running_agent_id, TerminateAgentRequest(), db_pool
        )
        forced = await ops_lifecycle.terminate_agent_op(
            running_agent_id, TerminateAgentRequest(force=True), db_pool
        )

        assert (graceful.status, graceful.shell_sessions) == ("enqueued", None)
        assert (forced.status, forced.shell_sessions) == ("enqueued", None)
        assert kills == []
        payloads = _terminate_payloads(db_conn, running_agent_id)
        assert len(payloads) == 2
        assert all(
            not isinstance(payload, dict) or "kill_all_shell_sessions" not in payload
            for payload in payloads
        )

    @pytest.mark.asyncio
    async def test_force_kills_right_away_and_reports_the_ids(
        self,
        db_conn: psycopg.Connection,
        db_pool: ConnectionPool,
        running_agent_id: int,
        kills: list[int],
    ) -> None:
        from ops import ops_lifecycle
        from ops.rpc_terminate import ShellSessionsKill

        resp = await ops_lifecycle.terminate_agent_op(
            running_agent_id,
            TerminateAgentRequest(force=True, kill_all_shell_sessions=True),
            db_pool,
        )

        assert resp.status == "enqueued"
        assert resp.shell_sessions == ShellSessionsKill(when="now", killed=[0, 3])
        assert kills == [running_agent_id]
        assert _terminate_payloads(db_conn, running_agent_id) == [{"kill_all_shell_sessions": True}]

    @pytest.mark.asyncio
    async def test_force_supersedes_a_graceful_kill_request_without_carrying_it(
        self,
        db_conn: psycopg.Connection,
        db_pool: ConnectionPool,
        running_agent_id: int,
        kills: list[int],
    ) -> None:
        """A force fence supersedes an unapplied graceful terminate, its kill
        request included — a force kills sessions only when asked itself (the
        watchdog's recovery force, for one, is followed by a resurrect)."""
        from ops import ops_lifecycle

        await ops_lifecycle.terminate_agent_op(
            running_agent_id, TerminateAgentRequest(kill_all_shell_sessions=True), db_pool
        )
        resp = await ops_lifecycle.terminate_agent_op(
            running_agent_id, TerminateAgentRequest(force=True), db_pool
        )

        assert resp.shell_sessions is None
        assert kills == []

    @pytest.mark.asyncio
    async def test_already_terminated_agent_is_killed_now(
        self, db_conn: psycopg.Connection, db_pool: ConnectionPool, kills: list[int]
    ) -> None:
        """The already-terminated form: no termination left to apply, the
        sessions are still killed and reported."""
        from ops import ops_lifecycle
        from ops.rpc_terminate import ShellSessionsKill

        agent_id = create_agent(db_conn)
        db_conn.execute(
            "INSERT INTO agents_meta (id,status,machine,termination_source) "
            "VALUES (%s,'terminated','test-machine','exit')",
            (agent_id,),
        )
        db_conn.commit()

        resp = await ops_lifecycle.terminate_agent_op(
            agent_id, TerminateAgentRequest(kill_all_shell_sessions=True), db_pool
        )

        assert resp.status == "already_terminated"
        assert resp.shell_sessions == ShellSessionsKill(when="now", killed=[0, 3])
        assert kills == [agent_id]
        assert _terminate_payloads(db_conn, agent_id) == []

    @pytest.mark.asyncio
    async def test_termination_racing_the_request_is_killed_now(
        self,
        db_conn: psycopg.Connection,
        db_pool: ConnectionPool,
        running_agent_id: int,
        kills: list[int],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The agent dies between the status read and the locked enqueue: the
        request is not queued onto a dead row no apply will read — its sessions
        are killed at once instead."""
        from ops import ops_lifecycle
        from shared.agents import AgentStatus

        def _stale_read(_aid: int) -> AgentStatus:
            return AgentStatus.RUNNING

        monkeypatch.setattr(ops_lifecycle, "get_agent_status", _stale_read)
        db_conn.execute(
            "UPDATE agents_meta SET status='terminated', termination_source='exit' WHERE id=%s",
            (running_agent_id,),
        )
        db_conn.commit()

        resp = await ops_lifecycle.terminate_agent_op(
            running_agent_id, TerminateAgentRequest(kill_all_shell_sessions=True), db_pool
        )

        assert resp.status == "already_terminated"
        assert resp.shell_sessions is not None and resp.shell_sessions.when == "now"
        assert kills == [running_agent_id]
        assert _terminate_payloads(db_conn, running_agent_id) == []

    @pytest.mark.asyncio
    async def test_a_failed_kill_fails_the_request_after_the_fence(
        self,
        db_conn: psycopg.Connection,
        db_pool: ConnectionPool,
        running_agent_id: int,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The termination is durable before the kill; a failed kill surfaces
        to the caller, whose repeat request retries the kill."""
        from ops import ops_lifecycle

        def _fail(_aid: int) -> list[int]:
            raise RuntimeError("failed to kill shell session(s) [2]")

        async def _noop_cancel(_aid: int, _command_id: int) -> None:
            return None

        monkeypatch.setattr(ops_lifecycle, "kill_agent_shells", _fail)
        monkeypatch.setattr(ops_lifecycle, "_cancel_hosted_turn_best_effort", _noop_cancel)

        with pytest.raises(RuntimeError, match="failed to kill"):
            await ops_lifecycle.terminate_agent_op(
                running_agent_id,
                TerminateAgentRequest(force=True, kill_all_shell_sessions=True),
                db_pool,
            )
        assert db_conn.execute(
            "SELECT status FROM agents_meta WHERE id=%s", (running_agent_id,)
        ).fetchone() == ("terminated",)


@pytest.mark.skipif(sys.platform == "win32", reason="real POSIX PTY sessions")
@pytest.mark.asyncio
async def test_kill_terminates_only_the_owners_real_shell_sessions(
    db_conn: psycopg.Connection,
    db_pool: ConnectionPool,
    running_agent_id: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End to end on real PTY sessions: `kill --kill-all-shell-sessions`
    kills the owner's shells and its watcher-shaped job session, and leaves
    another agent's session running."""
    from ops import ops_lifecycle
    from ops.rpc_terminate import ShellSessionsKill
    from shared.cluster import session_name
    from shared.session_backend import PtySessionBackend
    from shared.session_record import SessionRecord

    monkeypatch.setattr(settings.general, "ava_home", str(tmp_path))
    # The PTY CLI runs as a child process: it resolves its home from the
    # environment it inherits, not from this process's settings.
    monkeypatch.setitem(os.environ, "AVA_HOME", str(tmp_path))
    monkeypatch.setenv("AVA_HOME_OVERRIDE", "1")
    monkeypatch.setenv("HOME", str(tmp_path))

    async def _noop_cancel(_aid: int, _command_id: int) -> None:
        return None

    monkeypatch.setattr(ops_lifecycle, "_cancel_hosted_turn_best_effort", _noop_cancel)
    other_agent_id = create_agent(db_conn)
    owned = [
        session_name(f"agent-{running_agent_id}-shell-0"),
        session_name(f"agent-{running_agent_id}-shell-1-watcher"),
    ]
    foreign = session_name(f"agent-{other_agent_id}-shell-0")
    backend = PtySessionBackend()
    shell_pids: dict[str, int] = {}
    try:
        for name, cmd in ((owned[0], ""), (owned[1], "sleep 300"), (foreign, "")):
            assert backend.new_session(name, cmd, tmp_path, env={"AVA_HOME": str(tmp_path)})
            record = SessionRecord.read(tmp_path / "run" / "pty" / f"{name}.json")
            assert record is not None
            shell_pids[name] = record.pid
        job_pid = _wait_child(shell_pids[owned[1]])  # the watcher-shaped running job

        resp = await ops_lifecycle.terminate_agent_op(
            running_agent_id,
            TerminateAgentRequest(force=True, kill_all_shell_sessions=True),
            db_pool,
        )

        assert resp.shell_sessions == ShellSessionsKill(when="now", killed=[0, 1])
        assert set(backend.list_sessions()) == {foreign}
        assert not any(_pid_alive(shell_pids[name]) for name in owned)
        assert not _pid_alive(job_pid)
        assert _pid_alive(shell_pids[foreign])
        # An owner-level kill is silent: the killed running job leaves no notice.
        assert db_conn.execute(
            "SELECT kind FROM inbound_messages WHERE agent_id=%s", (running_agent_id,)
        ).fetchall() == [("terminate",)]
    finally:
        for name in backend.list_sessions():
            backend.kill_session(name)
    assert not any(_pid_alive(pid) for pid in shell_pids.values())


def _wait_child(pid: int, timeout: float = 15.0) -> int:
    """The first live child of `pid` once the shell has started its job."""
    import time

    import psutil

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        children = psutil.Process(pid).children(recursive=True)
        if children:
            return children[0].pid
        time.sleep(0.05)
    raise AssertionError(f"shell {pid} never started its job")


def _pid_alive(pid: int, timeout: float = 5.0) -> bool:
    """True while the process lives (a zombie counts as gone), within `timeout`."""
    import time

    import psutil

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if psutil.Process(pid).status() == psutil.STATUS_ZOMBIE:
                return False
        except psutil.NoSuchProcess:
            return False
        time.sleep(0.05)
    return True
