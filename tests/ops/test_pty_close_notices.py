"""Durable shell-closure notices (issue #2044) — journal + ops-daemon flush.

The stop path records one notice per busy session verified closed; the flush
delivers it exactly once to a live owner and drops it for a terminated one.
Every delivery claim is DB-atomic with the inbound insert, so a re-flush after
an interrupted deletion can never produce a duplicate inbound.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from ops import pty_close_notices as notices
from shared.config import settings
from shared.db import create_agent

_WHEN = datetime(2026, 9, 10, 1, 2, 3, tzinfo=UTC)


@pytest.fixture()
def journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(settings.general, "ava_home", str(tmp_path))
    return tmp_path


@pytest.fixture()
def pool() -> Iterator[ConnectionPool]:
    import shared.db

    p = shared.db.pool(max_size=2)
    yield p
    p.close()


def _record(
    *,
    agent_id: int = 7,
    session_id: int = 11,
    name: str | None = None,
    machine: str = "macmini",
    birth: str = "starttime:4242",
    pid: int = 9090,
) -> Path:
    path = notices.record_close(
        machine=machine,
        name=name or f"ava-agent-{agent_id}-shell-{session_id}-report",
        shell_pid=pid,
        shell_birth=birth,
        operation="local-pause:macmini:1:uuid",
        acquired_at=_WHEN,
    )
    assert path is not None
    return path


def _agent(db_conn: psycopg.Connection, status: str) -> int:
    agent_id = create_agent(db_conn)
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agents_meta (id, spawner, status) VALUES (%s, 'user', %s)",
            (agent_id, status),
        )
    db_conn.commit()
    return agent_id


def _inbounds(db_conn: psycopg.Connection, agent_id: int) -> list[tuple[str, str, str]]:
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT content, source, payload::text FROM inbound_messages WHERE agent_id = %s",
            (agent_id,),
        )
        return [(str(r[0]), str(r[1]), str(r[2])) for r in cur.fetchall()]


def test_record_close_writes_one_file_per_dedup_key(journal: Path) -> None:
    """Same (machine, agent, session, shell-birth) overwrites; a new birth is a
    new notice — a stop retry or CLI re-entry never stacks duplicates."""
    first = _record()
    assert _record() == first
    assert sorted(p.name for p in notices.journal_dir().iterdir()) == [first.name]
    second = _record(birth="starttime:9999")
    assert second != first
    assert len(list(notices.journal_dir().iterdir())) == 2


def test_record_close_rejects_non_agent_shell_names(journal: Path) -> None:
    """Sessions that are not agent-owned shells are never recorded."""
    assert (
        notices.record_close(
            machine="macmini",
            name="ava-gateway",
            shell_pid=1,
            shell_birth="birth:1.0",
            operation="local-pause:macmini:1:uuid",
            acquired_at=_WHEN,
        )
        is None
    )
    assert not notices.journal_dir().exists()


def test_flush_delivers_to_live_owner_once(
    db_conn: psycopg.Connection, pool: ConnectionPool, journal: Path
) -> None:
    aid = _agent(db_conn, "running")
    _record(agent_id=aid)
    assert notices.flush(pool) == 0
    rows = _inbounds(db_conn, aid)
    assert len(rows) == 1
    content, source, payload = rows[0]
    assert source == "system"
    assert "closed by an operator stop (ava stop)" in content
    assert "id 11" in content and "macmini" in content
    closure = json.loads(payload)["closure"]
    assert closure["agent_id"] == aid and closure["session_id"] == 11
    assert closure["operation"] == "local-pause:macmini:1:uuid"
    assert not list(notices.journal_dir().iterdir())


def test_flush_is_exactly_once_across_an_interrupted_deletion(
    db_conn: psycopg.Connection, pool: ConnectionPool, journal: Path
) -> None:
    """Replaying a delivered record (crash between commit and unlink) inserts
    nothing — the idempotency claim settles the re-flush."""
    aid = _agent(db_conn, "running")
    path = _record(agent_id=aid)
    assert notices.flush(pool) == 0
    # Simulate the interrupted deletion: the record file is still there.
    _record(agent_id=aid)
    assert notices.flush(pool) == 0
    assert len(_inbounds(db_conn, aid)) == 1
    assert not path.exists()


def test_flush_drops_terminated_owner_without_inbound(
    db_conn: psycopg.Connection, pool: ConnectionPool, journal: Path
) -> None:
    """A closure notice must never resurrect a dead agent (TTL boundary)."""
    aid = _agent(db_conn, "terminated")
    _record(agent_id=aid)
    assert notices.flush(pool) == 0
    assert _inbounds(db_conn, aid) == []
    assert not list(notices.journal_dir().iterdir())


def test_flush_drops_unknown_agent(
    db_conn: psycopg.Connection, pool: ConnectionPool, journal: Path
) -> None:
    _record(agent_id=99999)
    assert notices.flush(pool) == 0
    assert not list(notices.journal_dir().iterdir())
    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM inbound_messages WHERE agent_id = 99999")
        row = cur.fetchone()
        assert row is not None and row[0] == 0


def test_flush_keeps_record_when_delivery_fails(
    db_conn: psycopg.Connection,
    pool: ConnectionPool,
    journal: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed delivery stays visible and retryable — never silently dropped."""
    aid = _agent(db_conn, "running")
    _record(agent_id=aid)

    def _boom(*_args: object, **_kwargs: object) -> int:
        raise RuntimeError("db down")

    original = notices.insert_inbound_message
    monkeypatch.setattr(notices, "insert_inbound_message", _boom)
    assert notices.flush(pool) == 1
    assert len(list(notices.journal_dir().iterdir())) == 1
    # Restore only the patched function — `monkeypatch.undo()` would also
    # revert the journal fixture's ava_home redirect.
    monkeypatch.setattr(notices, "insert_inbound_message", original)
    assert notices.flush(pool) == 0
    assert len(_inbounds(db_conn, aid)) == 1


def test_flush_keeps_unreadable_record(journal: Path, pool: ConnectionPool) -> None:
    notices.journal_dir().mkdir(parents=True, exist_ok=True)
    bad = notices.journal_dir() / "3_5_deadbeef.json"
    bad.write_text("{not json", encoding="utf-8")
    assert notices.flush(pool) == 1
    assert bad.exists()
