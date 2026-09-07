"""Atomic persistence and failure-priority tests for termination messages."""

from __future__ import annotations

from collections.abc import Iterator
from typing import cast

import psycopg
import pytest
from psycopg_pool import ConnectionPool
from pydantic import ValidationError

from ops import ops_exit
from ops.rpc_schemas import TerminateAgentRequest
from shared.config import settings
from shared.db import create_agent


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
    ) -> tuple[int | None, int]:
        nonlocal failed_pair
        result = real_insert(conn, agent_id, source=source, message=message)
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
    ) -> tuple[int | None, int]:
        if message is not None:
            raise RuntimeError("injected pair failure")
        return real_insert(conn, agent_id, source=source, message=message)

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
    ) -> tuple[int | None, int]:
        nonlocal failed_pair
        result = real_insert(conn, agent_id, source=source, message=message)
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
