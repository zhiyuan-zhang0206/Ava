"""The service's crash-notice child (issue #2044; task #4980).

`send` stages what a start-time sweep closed on disk before running the
one-shot child, so a child cut by the time limit or failing on the database
leaves the batch for the next start to re-send — reported at ERROR with its
count — and the idempotency keys keep the re-send from delivering twice.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest

from base.db import create_agent
from base.native_process.ownership import OwnedProcess
from base.sessions.pty import closure
from base.sessions.pty.paths import close_notices_path
from ops import pty_close_notices
from services.pty_sessions import crash_notices


@pytest.fixture
def staged() -> Iterator[Path]:
    """The staged notices file of this session's home, absent before and after the test."""
    path = close_notices_path()
    path.unlink(missing_ok=True)
    try:
        yield path
    finally:
        path.unlink(missing_ok=True)


def _agent(db_conn: psycopg.Connection, status: str) -> int:
    agent_id = create_agent(db_conn)
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agents_meta (id, spawner, status) VALUES (%s, 'user', %s)",
            (agent_id, status),
        )
    db_conn.commit()
    return agent_id


def _sweep(*closed: closure.ClosedSession) -> closure.Outcome:
    return closure.Outcome(closed=tuple(closed))


def _busy(agent_id: int, session_id: int) -> closure.ClosedSession:
    return closure.ClosedSession(
        f"ava-agent-{agent_id}-shell-{session_id}-swept",
        OwnedProcess(9090 + session_id, 1.0, session_id),
    )


def _inbound_sessions(db_conn: psycopg.Connection, agent_id: int) -> list[int]:
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT payload->'closure'->>'session_id' FROM inbound_messages WHERE agent_id = %s",
            (agent_id,),
        )
        return sorted(int(str(row[0])) for row in cur.fetchall())


async def test_send_stages_and_delivers_a_hundred_session_sweep(
    db_conn: psycopg.Connection, staged: Path
) -> None:
    """End to end through the real child: a sweep of 100 busy sessions (past the
    incident's ~38 truncation point) lands every notice and leaves no staged file."""
    live, second, dead = (
        _agent(db_conn, "running"),
        _agent(db_conn, "running"),
        _agent(db_conn, "terminated"),
    )
    swept = _sweep(
        *[_busy(live, 1000 + n) for n in range(50)],
        *[_busy(second, 2000 + n) for n in range(45)],
        *[_busy(dead, 3000 + n) for n in range(5)],
    )

    await crash_notices.send(swept)

    assert _inbound_sessions(db_conn, live) == [1000 + n for n in range(50)]
    assert len(_inbound_sessions(db_conn, second)) == 45
    assert _inbound_sessions(db_conn, dead) == []
    assert not staged.exists(), "a delivered batch leaves no staged file"


async def test_a_child_cut_by_the_time_limit_is_loud_and_the_next_start_delivers_it_once(
    db_conn: psycopg.Connection,
    staged: Path,
    monkeypatch: pytest.MonkeyPatch,
    loguru_records: list[dict[str, Any]],
) -> None:
    """The incident's shape: the child is killed at `NOTICE_LIMIT_S` before it writes
    anything. The batch stays staged — nothing is lost — and the next start re-sends
    it on the same idempotency keys: every notice lands exactly once."""
    live = _agent(db_conn, "running")
    swept = _sweep(*[_busy(live, 1000 + n) for n in range(100)])
    monkeypatch.setattr(crash_notices, "NOTICE_LIMIT_S", 0.3)

    await crash_notices.send(
        swept, child_command=(sys.executable, "-c", "import time; time.sleep(30)")
    )

    assert len(pty_close_notices.read_pending(staged)) == 100, "the cut batch stays staged"
    assert _inbound_sessions(db_conn, live) == []
    assert any(
        record["level"].name == "ERROR"
        and "timed out" in record["message"]
        and "100 notice(s) stay staged" in record["message"]
        for record in loguru_records
    )

    monkeypatch.setattr(crash_notices, "NOTICE_LIMIT_S", 60.0)
    await crash_notices.send(swept)

    assert _inbound_sessions(db_conn, live) == [1000 + n for n in range(100)]
    assert not staged.exists()


async def test_a_failing_child_reports_the_leftover_batch_and_keeps_it_staged(
    db_conn: psycopg.Connection,
    staged: Path,
    loguru_records: list[dict[str, Any]],
) -> None:
    """A child that exits non-zero (a database that answers nothing) is reported at
    ERROR with the count, and its batch waits for the next start."""
    live = _agent(db_conn, "running")
    swept = _sweep(*[_busy(live, 1000 + n) for n in range(3)])

    await crash_notices.send(swept, child_command=(sys.executable, "-c", "raise SystemExit(1)"))

    assert len(pty_close_notices.read_pending(staged)) == 3
    assert any(
        record["level"].name == "ERROR"
        and "not all written (exit 1)" in record["message"]
        and "3 notice(s) stay staged" in record["message"]
        for record in loguru_records
    )


async def test_a_clean_start_stages_nothing_and_runs_no_child(
    staged: Path, loguru_records: list[dict[str, Any]]
) -> None:
    """No swept sessions and nothing waiting: no child runs and nothing is reported;
    a child that ran would exit non-zero and be loud."""
    await crash_notices.send(
        closure.Outcome(), child_command=(sys.executable, "-c", "raise SystemExit(7)")
    )

    assert not staged.exists()
    assert [r for r in loguru_records if r["level"].name in ("ERROR", "WARNING")] == []
