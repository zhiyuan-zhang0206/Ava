"""Native control acceptance is atomic and survives mutable target/queue state."""

from concurrent.futures import ThreadPoolExecutor
from time import monotonic, sleep
from typing import Any

import psycopg
import pytest
from psycopg import sql
from psycopg_pool import ConnectionPool

from base.agents import AgentNotFound
from base.agents.contract import CancelResult
from base.agents.messages import control_delivery
from base.agents.messages.control_delivery import ControlConflictError, accept_control
from base.agents.messages.inbound import InboundKind
from base.config import settings


@pytest.fixture
def pool():
    with ConnectionPool(settings.data_plane.db_url, min_size=1, max_size=3) as pool:
        yield pool


def _agent(conn: psycopg.Connection, status: str = "running") -> int:
    row = conn.execute("INSERT INTO agents DEFAULT VALUES RETURNING id").fetchone()
    assert row is not None
    conn.execute(
        "INSERT INTO agents_meta(id,spawner,status) VALUES (%s,'test',%s)", (row[0], status)
    )
    conn.commit()
    return row[0]


@pytest.mark.parametrize("kind", [InboundKind.CANCEL, InboundKind.COMPACT_REQUEST])
def test_competing_acceptances_share_original_inbound(
    pool: ConnectionPool, db_conn: psycopg.Connection, kind: control_delivery.ControlKind
) -> None:
    agent = _agent(db_conn)
    with ThreadPoolExecutor(max_workers=4) as workers:
        accepted = [
            workers.submit(accept_control, pool, agent, kind, path="/control", key="once")
            for _ in range(4)
        ]
        receipts = [future.result(timeout=10) for future in accepted]
    assert len({receipt.inbound_id for receipt in receipts}) == 1
    assert sum(receipt.inserted for receipt in receipts) == 1
    assert db_conn.execute(
        "SELECT count(*) FROM inbound_messages WHERE agent_id=%s", (agent,)
    ).fetchone() == (1,)
    assert db_conn.execute("SELECT count(*) FROM agent_control_receipts").fetchone() == (1,)
    event = "compact" if kind is InboundKind.COMPACT_REQUEST else "cancel"
    assert db_conn.execute(
        "SELECT count(*) FROM audit_events WHERE agent_id=%s AND event_name=%s", (agent, event)
    ).fetchone() == (1,)


def test_noop_replays_after_resurrection_and_changed_request_conflicts(
    pool: ConnectionPool, db_conn: psycopg.Connection
) -> None:
    agent = _agent(db_conn, "terminated")
    first = accept_control(pool, agent, InboundKind.CANCEL, path="/api/cancel", key="once")
    assert first.status is CancelResult.ALREADY_TERMINATED and first.inbound_id is None
    db_conn.execute(
        "UPDATE agents_meta SET status='running',termination_source=NULL WHERE id=%s", (agent,)
    )
    db_conn.commit()
    replay = accept_control(pool, agent, InboundKind.CANCEL, path="/api/cancel", key="once")
    assert replay.status is first.status and not replay.pending and not replay.inserted
    other = _agent(db_conn)
    with pytest.raises(ControlConflictError):
        accept_control(pool, other, InboundKind.CANCEL, path="/api/cancel", key="once")
    assert db_conn.execute("SELECT count(*) FROM inbound_messages").fetchone() == (0,)
    assert (
        accept_control(pool, agent, InboundKind.CANCEL, path="/api/cancel", key="new").inbound_id
        is not None
    )


@pytest.mark.parametrize("remove", [False, True])
def test_consumed_or_deleted_inbound_is_not_requeued(
    pool: ConnectionPool, db_conn: psycopg.Connection, remove: bool
) -> None:
    agent = _agent(db_conn)
    first = accept_control(pool, agent, InboundKind.COMPACT_REQUEST, path="/compact", key="once")
    if remove:
        db_conn.execute("DELETE FROM inbound_messages WHERE id=%s", (first.inbound_id,))
    else:
        db_conn.execute(
            "UPDATE inbound_messages SET status='done' WHERE id=%s", (first.inbound_id,)
        )
    db_conn.commit()
    replay = accept_control(pool, agent, InboundKind.COMPACT_REQUEST, path="/compact", key="once")
    assert replay.inbound_id == first.inbound_id and not replay.pending and not replay.inserted
    assert db_conn.execute(
        "SELECT count(*) FROM inbound_messages WHERE status='pending'"
    ).fetchone() == (0,)


@pytest.mark.parametrize("kind", [InboundKind.CANCEL, InboundKind.COMPACT_REQUEST])
def test_producer_failure_rolls_back_inbound_audit_and_receipt(
    pool: ConnectionPool,
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    kind: control_delivery.ControlKind,
) -> None:
    agent = _agent(db_conn)
    insert = control_delivery.insert_inbound_message_in_transaction

    def fail_after_insert(*args: Any, **kwargs: Any) -> Any:
        insert(*args, **kwargs)
        raise psycopg.OperationalError("producer stopped before receipt")

    with monkeypatch.context() as injected:
        injected.setattr(
            control_delivery, "insert_inbound_message_in_transaction", fail_after_insert
        )
        with pytest.raises(psycopg.OperationalError):
            accept_control(pool, agent, kind, path="/control", key="once")
    for table in ("inbound_messages", "audit_events", "agent_control_receipts"):
        assert db_conn.execute(
            sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table))
        ).fetchone() == (0,)
    assert accept_control(pool, agent, kind, path="/control", key="once").inserted


def test_cancel_checks_status_after_waiting_for_termination_lock(
    pool: ConnectionPool, db_conn: psycopg.Connection
) -> None:
    agent = _agent(db_conn)
    db_conn.execute(
        "UPDATE agents_meta SET status='terminated',termination_source='user' WHERE id=%s", (agent,)
    )
    with ThreadPoolExecutor(max_workers=1) as workers:
        future = workers.submit(
            accept_control, pool, agent, InboundKind.CANCEL, path="/api/cancel", key="once"
        )
        try:
            deadline = monotonic() + 5
            while monotonic() < deadline:
                with pool.connection() as observer:
                    blocked = observer.execute(
                        "SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE %s=ANY(pg_blocking_pids(pid)))",
                        (db_conn.info.backend_pid,),
                    ).fetchone()
                if blocked == (True,):
                    break
                sleep(0.01)
            else:
                raise AssertionError("cancel did not wait for the status owner")
        finally:
            db_conn.commit()
        receipt = future.result(timeout=10)
    assert receipt.status is CancelResult.ALREADY_TERMINATED and receipt.inbound_id is None
    assert db_conn.execute("SELECT count(*) FROM inbound_messages").fetchone() == (0,)


def test_missing_agent_is_not_accepted(pool: ConnectionPool, db_conn: psycopg.Connection) -> None:
    with pytest.raises(AgentNotFound):
        accept_control(pool, 99999, InboundKind.CANCEL, path="/api/cancel", key="once")
    assert db_conn.execute("SELECT count(*) FROM agent_control_receipts").fetchone() == (0,)


def test_deleted_agent_noop_keeps_original_snapshot(
    pool: ConnectionPool, db_conn: psycopg.Connection
) -> None:
    agent = _agent(db_conn, "terminated")
    first = accept_control(pool, agent, InboundKind.CANCEL, path="/api/cancel", key="gone")
    db_conn.execute("DELETE FROM agents_meta WHERE id=%s", (agent,))
    db_conn.execute("DELETE FROM agents WHERE id=%s", (agent,))
    db_conn.commit()
    replay = accept_control(pool, agent, InboundKind.CANCEL, path="/api/cancel", key="gone")
    assert replay.agent_id == first.agent_id and replay.status is first.status
    assert replay.inbound_id is None and not replay.pending
