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


class TestFinalTerminationClosureMarker:
    """`terminate --final` stamps `agents_meta.closed_at` in the same
    transaction as the termination intent (graceful acceptance / force fence),
    and the metadata-only mark covers an already-dead row (the backfill
    route). Without `final` the marker stays NULL — ordinary termination is
    unchanged."""

    def test_graceful_final_stamps_the_marker(
        self, db_conn: psycopg.Connection, db_pool: ConnectionPool, running_agent_id: int
    ) -> None:
        ops_exit._enqueue_termination_inbounds(
            running_agent_id, db_pool, source="user", message=None, final=True
        )
        assert db_conn.execute(
            "SELECT closed_at IS NOT NULL FROM agents_meta WHERE id=%s", (running_agent_id,)
        ).fetchone() == (True,)

    def test_graceful_default_leaves_the_marker_open(
        self, db_conn: psycopg.Connection, db_pool: ConnectionPool, running_agent_id: int
    ) -> None:
        ops_exit._enqueue_termination_inbounds(
            running_agent_id, db_pool, source="user", message=None
        )
        assert db_conn.execute(
            "SELECT closed_at FROM agents_meta WHERE id=%s", (running_agent_id,)
        ).fetchone() == (None,)

    def test_force_final_stamps_the_marker(
        self, db_conn: psycopg.Connection, db_pool: ConnectionPool, running_agent_id: int
    ) -> None:
        ops_exit._force_terminate_transaction(running_agent_id, db_pool, source="user", final=True)
        assert db_conn.execute(
            "SELECT status, closed_at IS NOT NULL FROM agents_meta WHERE id=%s",
            (running_agent_id,),
        ).fetchone() == ("terminated", True)

    @pytest.mark.asyncio
    async def test_force_final_records_closed_in_the_terminate_event(
        self,
        db_conn: psycopg.Connection,
        db_pool: ConnectionPool,
        running_agent_id: int,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The force path's audit event names the closure — parity with the
        graceful path (`_enqueue_termination_inbounds` passes `closed=final`).
        `terminate --final` / `kill --final` are force-first use cases, so the
        `terminate` event must carry `closed: true` next to the command it
        accompanied (`test_force_final_stamps_the_marker` pins the marker)."""
        from ops import ops_lifecycle

        events: list[dict[str, object]] = []

        def _record(**kwargs: object) -> None:
            events.append(kwargs)

        monkeypatch.setattr(ops_exit, "insert_event_log", _record)

        async def _noop_cancel(_aid: int, _command_id: int) -> None:
            return None

        monkeypatch.setattr(ops_lifecycle, "_cancel_hosted_turn_best_effort", _noop_cancel)

        resp = await ops_lifecycle.terminate_agent_op(
            running_agent_id, TerminateAgentRequest(force=True, final=True), db_pool
        )
        assert resp.status == "enqueued"
        row = db_conn.execute(
            "SELECT last_force_terminate_inbound_id FROM agents_meta WHERE id=%s",
            (running_agent_id,),
        ).fetchone()
        assert row is not None
        (inbound_id,) = row
        assert events == [
            {
                "event_type": "terminate",
                "agent_id": running_agent_id,
                "source": "user",
                "payload": {"inbound_id": inbound_id, "closed": True},
            }
        ]

    def test_repeat_close_keeps_the_first_time(
        self, db_conn: psycopg.Connection, db_pool: ConnectionPool, running_agent_id: int
    ) -> None:
        """A second close (e.g. a `--final` kill after a graceful `--final`)
        never churns the closure timestamp, and the metadata-only mark reports
        it was already closed."""
        ops_exit._enqueue_termination_inbounds(
            running_agent_id, db_pool, source="user", message=None, final=True
        )
        first = db_conn.execute(
            "SELECT closed_at FROM agents_meta WHERE id=%s", (running_agent_id,)
        ).fetchone()
        assert first is not None and first[0] is not None

        assert ops_exit.mark_agent_closed(running_agent_id, source="user", db_pool=db_pool) is False
        assert (
            db_conn.execute(
                "SELECT closed_at FROM agents_meta WHERE id=%s", (running_agent_id,)
            ).fetchone()
            == first
        )

    def test_mark_agent_closed_audits_once(
        self,
        db_conn: psycopg.Connection,
        db_pool: ConnectionPool,
        running_agent_id: int,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        db_conn.execute(
            "UPDATE agents_meta SET status='terminated', termination_source='exit' WHERE id=%s",
            (running_agent_id,),
        )
        db_conn.commit()
        events: list[dict[str, object]] = []

        def _record(**kwargs: object) -> None:
            events.append(kwargs)

        monkeypatch.setattr(ops_exit, "insert_event_log", _record)

        assert ops_exit.mark_agent_closed(running_agent_id, source="user", db_pool=db_pool) is True
        assert ops_exit.mark_agent_closed(running_agent_id, source="user", db_pool=db_pool) is False
        assert events == [
            {
                "event_type": "terminate",
                "agent_id": running_agent_id,
                "source": "user",
                "payload": {"closed": True},
            }
        ]

    @pytest.mark.asyncio
    async def test_terminate_op_marks_closed_on_an_already_dead_row(
        self, db_conn: psycopg.Connection, db_pool: ConnectionPool
    ) -> None:
        """The graceful short-circuit still honors `final`: an already-dead
        agent is marked metadata-only — the backfill route."""
        from ops import ops_lifecycle

        agent_id = create_agent(db_conn)
        db_conn.execute(
            "INSERT INTO agents_meta (id,status,machine,termination_source) "
            "VALUES (%s,'terminated','test-machine','exit')",
            (agent_id,),
        )
        db_conn.commit()

        resp = await ops_lifecycle.terminate_agent_op(
            agent_id, TerminateAgentRequest(final=True), db_pool
        )
        assert resp.status == "already_terminated"
        assert resp.closed is True
        assert db_conn.execute(
            "SELECT closed_at IS NOT NULL FROM agents_meta WHERE id=%s", (agent_id,)
        ).fetchone() == (True,)

    @pytest.mark.asyncio
    async def test_terminate_op_reports_closed_false_for_an_unclosed_dead_row(
        self, db_conn: psycopg.Connection, db_pool: ConnectionPool
    ) -> None:
        """A dead row without the marker reports closed false and stays open —
        a plain terminate never closes; only `--final` (or the backfill mark) does."""
        from ops import ops_lifecycle

        agent_id = create_agent(db_conn)
        db_conn.execute(
            "INSERT INTO agents_meta (id,status,machine,termination_source) "
            "VALUES (%s,'terminated','test-machine','exit')",
            (agent_id,),
        )
        db_conn.commit()

        resp = await ops_lifecycle.terminate_agent_op(agent_id, TerminateAgentRequest(), db_pool)
        assert resp.status == "already_terminated"
        assert resp.closed is False
        assert db_conn.execute(
            "SELECT closed_at FROM agents_meta WHERE id=%s", (agent_id,)
        ).fetchone() == (None,)

    @pytest.mark.asyncio
    async def test_terminate_op_reports_closed_on_an_alive_row(
        self, db_conn: psycopg.Connection, db_pool: ConnectionPool, running_agent_id: int
    ) -> None:
        """Alive rows: a plain terminate reports closed false and stamps nothing;
        `--final` reports closed true (its stamp rides the same transaction)."""
        from ops import ops_lifecycle

        plain = await ops_lifecycle.terminate_agent_op(
            running_agent_id, TerminateAgentRequest(), db_pool
        )
        assert plain.status == "enqueued"
        assert plain.closed is False
        assert db_conn.execute(
            "SELECT closed_at FROM agents_meta WHERE id=%s", (running_agent_id,)
        ).fetchone() == (None,)

        final = await ops_lifecycle.terminate_agent_op(
            running_agent_id, TerminateAgentRequest(final=True), db_pool
        )
        assert final.status == "enqueued"
        assert final.closed is True
        assert db_conn.execute(
            "SELECT closed_at IS NOT NULL FROM agents_meta WHERE id=%s", (running_agent_id,)
        ).fetchone() == (True,)
