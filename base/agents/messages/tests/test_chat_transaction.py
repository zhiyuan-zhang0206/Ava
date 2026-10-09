"""Caller-owned transactions compose chat identity and audit facts without dispatch."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import psycopg
import pytest

from base import telemetry
from base.agents.messages.chat_delivery import (
    ChatInboundReceipt,
    ClientMessageConflictError,
    insert_chat_inbound_in_transaction,
    insert_chat_inbound_once,
)
from base.db import Database, create_agent
from base.db.transaction import write_transaction


def counts(conn: psycopg.Connection, agent_id: int) -> tuple[int, int]:
    row = conn.execute(
        "SELECT (SELECT count(*) FROM inbound_messages WHERE agent_id=%s), "
        "(SELECT count(*) FROM audit_events WHERE agent_id=%s AND event_name='send_message')",
        (agent_id, agent_id),
    ).fetchone()
    assert row is not None
    return row[0], row[1]


def write(
    conn: psycopg.Connection, agent_id: int, sender: int, *, content: str = "original"
) -> tuple[ChatInboundReceipt, telemetry.Event | None]:
    return insert_chat_inbound_in_transaction(
        conn,
        agent_id=agent_id,
        content=content,
        source=f"agent:{sender}",
        payload=None,
        client_message_id="caller-owned-chat",
    )


def test_caller_rollback_removes_chat_audit_and_other_business_write(
    db_conn: psycopg.Connection, database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent_id, sender = create_agent(db_conn), create_agent(db_conn)
    original_label = db_conn.execute("SELECT label FROM agents WHERE id=%s", (agent_id,)).fetchone()
    emitted: list[telemetry.Event] = []
    monkeypatch.setattr(telemetry, "emit_prepared", emitted.append)

    with (
        database.pool(min_size=1, max_size=2) as pool,
        pytest.raises(RuntimeError, match="domain receipt refused"),
        write_transaction(pool) as conn,
    ):
        conn.execute("UPDATE agents SET label='uncommitted' WHERE id=%s", (agent_id,))
        receipt, event = write(conn, agent_id, sender)
        assert receipt.inserted and event is not None
        assert counts(conn, agent_id) == (1, 1)
        assert counts(db_conn, agent_id) == (0, 0)
        assert emitted == []
        raise RuntimeError("domain receipt refused")

    assert counts(db_conn, agent_id) == (0, 0)
    assert (
        db_conn.execute("SELECT label FROM agents WHERE id=%s", (agent_id,)).fetchone()
        == original_label
    )
    assert emitted == []


def test_caller_commit_makes_chat_and_audit_visible_together(
    db_conn: psycopg.Connection, database: Database
) -> None:
    agent_id, sender = create_agent(db_conn), create_agent(db_conn)
    with database.pool(min_size=1, max_size=2) as pool:
        with write_transaction(pool) as conn:
            receipt, event = write(conn, agent_id, sender)
            assert receipt.inserted and event is not None
            assert counts(db_conn, agent_id) == (0, 0)
        assert counts(db_conn, agent_id) == (1, 1)

        with write_transaction(pool) as conn:
            replay, repeated_event = write(conn, agent_id, sender)
            assert replay.inbound_id == receipt.inbound_id
            assert not replay.inserted and replay.pending
            assert repeated_event is None
    assert counts(db_conn, agent_id) == (1, 1)


def test_changed_identity_rolls_back_the_callers_other_write(
    db_conn: psycopg.Connection, database: Database
) -> None:
    agent_id, sender = create_agent(db_conn), create_agent(db_conn)
    original_label = db_conn.execute("SELECT label FROM agents WHERE id=%s", (agent_id,)).fetchone()
    with database.pool(min_size=1, max_size=2) as pool:
        with write_transaction(pool) as conn:
            write(conn, agent_id, sender)
        with (
            pytest.raises(ClientMessageConflictError, match="content"),
            write_transaction(pool) as conn,
        ):
            conn.execute("UPDATE agents SET label='wrong' WHERE id=%s", (agent_id,))
            write(conn, agent_id, sender, content="changed")
    assert counts(db_conn, agent_id) == (1, 1)
    assert (
        db_conn.execute("SELECT label FROM agents WHERE id=%s", (agent_id,)).fetchone()
        == original_label
    )


def test_concurrent_callers_commit_one_chat_and_one_audit(
    db_conn: psycopg.Connection, database: Database
) -> None:
    agent_id, sender = create_agent(db_conn), create_agent(db_conn)
    barrier = Barrier(2)
    with database.pool(min_size=2, max_size=2) as pool:

        def accept(_index: int) -> ChatInboundReceipt:
            with write_transaction(pool) as conn:
                barrier.wait(timeout=5)
                receipt, _event = write(conn, agent_id, sender)
                return receipt

        with ThreadPoolExecutor(max_workers=2) as executor:
            receipts = list(executor.map(accept, range(2)))
    assert receipts[0].inbound_id == receipts[1].inbound_id
    assert sum(receipt.inserted for receipt in receipts) == 1
    assert counts(db_conn, agent_id) == (1, 1)


@pytest.mark.parametrize("autocommit", [False, True])
def test_writer_refuses_an_idle_connection_before_any_effect(
    db_conn: psycopg.Connection, database: Database, autocommit: bool
) -> None:
    agent_id, sender = create_agent(db_conn), create_agent(db_conn)
    with (
        database.pool(min_size=1, max_size=1, autocommit=autocommit) as pool,
        pool.connection() as conn,
        pytest.raises(RuntimeError, match="caller-owned transaction"),
    ):
        write(conn, agent_id, sender)
    assert counts(db_conn, agent_id) == (0, 0)


def test_compatibility_wrapper_dispatches_after_commit_and_wakes_only_first_insert(
    db_conn: psycopg.Connection, database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent_id, sender = create_agent(db_conn), create_agent(db_conn)
    observed: list[str] = []

    def emit(_event: telemetry.Event) -> None:
        assert counts(db_conn, agent_id) == (1, 1)
        observed.append("audit")

    def wake(target: int, _payload: str) -> bool:
        assert target == agent_id and counts(db_conn, agent_id) == (1, 1)
        observed.append("wake")
        return True

    monkeypatch.setattr(telemetry, "emit_prepared", emit)
    with database.pool(min_size=1, max_size=1) as pool, pool.connection() as conn:
        for _attempt in range(2):
            insert_chat_inbound_once(
                conn,
                agent_id=agent_id,
                content="original",
                source=f"agent:{sender}",
                payload=None,
                client_message_id="caller-owned-chat",
                publish_wake=wake,
            )
    assert observed == ["audit", "wake"]


@pytest.mark.parametrize("step", ["audit", "wake"])
def test_postcommit_failure_carries_receipt_without_repeating_audit(
    db_conn: psycopg.Connection, database: Database, monkeypatch: pytest.MonkeyPatch, step: str
) -> None:
    from base.agents.messages.chat_delivery import ChatInboundCommittedError

    agent_id, sender = create_agent(db_conn), create_agent(db_conn)
    bug = AttributeError("postcommit bug")

    def emit(_event: telemetry.Event) -> None:
        if step == "audit":
            raise bug

    def wake(_target: int, _payload: str) -> bool:
        if step == "wake":
            raise bug
        return True

    monkeypatch.setattr(telemetry, "emit_prepared", emit)
    with database.pool(min_size=1, max_size=1) as pool, pool.connection() as conn:
        with pytest.raises(ChatInboundCommittedError) as failed:
            insert_chat_inbound_once(
                conn,
                agent_id=agent_id,
                content="original",
                source=f"agent:{sender}",
                payload=None,
                client_message_id="caller-owned-chat",
                publish_wake=wake,
            )
        assert failed.value.__cause__ is bug
        assert failed.value.client_message_id == "caller-owned-chat"
        assert failed.value.receipt.inserted
        assert counts(db_conn, agent_id) == (1, 1)
        recovered = insert_chat_inbound_once(
            conn,
            agent_id=agent_id,
            content="original",
            source=f"agent:{sender}",
            payload=None,
            client_message_id="caller-owned-chat",
            publish_wake=wake,
        )
    assert recovered.inbound_id == failed.value.receipt.inbound_id
    assert not recovered.inserted
    assert counts(db_conn, agent_id) == (1, 1)
