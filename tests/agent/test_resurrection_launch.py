"""Committed launch budgets and delayed admission use the same actual PG rows."""

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from agent._starting import claim_agent_row
from ops import agent_launch, agent_wake
from ops.ops_lifecycle import _pending_allocation_can_resume
from shared.agents import ResurrectError
from shared.config import settings
from shared.db import PG_KEEPALIVE_KWARGS, insert_inbound_message
from shared.resurrection_launch import authorize_launch, pending_allocation, prepare_launch
from tests.agent.test_runtime_incarnation import _row


def _prepare(conn: psycopg.Connection, agent_id: int, wake: int) -> int:
    conn.execute("SELECT id FROM agents_meta WHERE id=%s FOR UPDATE", (agent_id,))
    epoch = conn.execute(
        "SELECT status_changed_at FROM agents_meta WHERE id=%s", (agent_id,)
    ).fetchone()
    assert epoch is not None
    marker = conn.execute(
        "INSERT INTO inbound_messages(agent_id,content,kind,source) "
        "VALUES(%s,'','resurrect','user') RETURNING id",
        (agent_id,),
    ).fetchone()
    assert marker is not None
    prepare_launch(conn, agent_id, marker[0], epoch[0], wake)
    conn.commit()
    return marker[0]


def test_launch_crash_and_redispatch_cannot_reset_wake_budget(db_conn: psycopg.Connection) -> None:
    agent_id = _row(db_conn)
    wake = insert_inbound_message(db_conn, agent_id, "wake", "user")
    db_conn.commit()
    first = _prepare(db_conn, agent_id, wake)
    db_conn.execute("SELECT id FROM agents_meta WHERE id=%s FOR UPDATE", (agent_id,))
    assert authorize_launch(db_conn, agent_id, first, 2)[0] == 1
    db_conn.commit()  # Controller dies here, before any OS launch.
    second = _prepare(db_conn, agent_id, wake)
    db_conn.execute("SELECT id FROM agents_meta WHERE id=%s FOR UPDATE", (agent_id,))
    assert authorize_launch(db_conn, agent_id, second, 2)[0] == 2
    db_conn.commit()  # A launch failure cannot refund this committed attempt.
    db_conn.execute("SELECT id FROM agents_meta WHERE id=%s FOR UPDATE", (agent_id,))
    with pytest.raises(ResurrectError, match="budget exhausted"):
        authorize_launch(db_conn, agent_id, second, 2)
    assert db_conn.execute(
        "SELECT status,payload->'resurrection_launch_attempts' FROM inbound_messages WHERE id=%s",
        (wake,),
    ).fetchone() == ("pending", 2)
    db_conn.commit()


def test_prepared_crash_resumes_same_allocation_before_os_launch(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent_id = _row(db_conn)
    wake = insert_inbound_message(db_conn, agent_id, "wake", "user")
    db_conn.commit()
    command = _prepare(db_conn, agent_id, wake)
    before = db_conn.execute(
        "SELECT status_changed_at FROM agents_meta WHERE id=%s", (agent_id,)
    ).fetchone()
    assert _pending_allocation_can_resume(agent_id, wake)
    assert not _pending_allocation_can_resume(agent_id, None)
    from services.delivery_watchdog.daemon import select_terminated_owners_with_pending

    with ConnectionPool[psycopg.Connection](
        settings.data_plane.db_url, min_size=1, max_size=1, kwargs=PG_KEEPALIVE_KWARGS
    ) as pool:
        assert (agent_id, wake) in select_terminated_owners_with_pending(pool)
    prepared = agent_wake._resume_pending_allocation(agent_id, wake)
    assert prepared is not None and prepared.command_id == command
    calls: list[int] = []

    def failed_launch(*args: object, **kwargs: object) -> str:
        # Separate connection sees authorization before a real launcher could
        # fork: no uncommitted row lock or rolled-back "spent" counter.
        db_conn.rollback()
        count = db_conn.execute(
            "SELECT payload->'resurrection_launch_attempts' FROM inbound_messages WHERE id=%s",
            (wake,),
        ).fetchone()
        assert count == (1,)
        attempt = kwargs["resurrect_attempt"]
        assert isinstance(attempt, tuple)
        assert attempt[:2] == (command, 1)
        calls.append(1)
        raise RuntimeError("injected failure after committed authorization")

    monkeypatch.setattr(agent_launch, "_launch_agent_process", failed_launch)
    with pytest.raises(RuntimeError, match="injected failure"):
        agent_wake._retry_resurrect_session(agent_id, prepared)
    assert len(calls) == 1
    assert (
        db_conn.execute(
            "SELECT status_changed_at FROM agents_meta WHERE id=%s", (agent_id,)
        ).fetchone()
        == before
    )
    assert pending_allocation(db_conn, agent_id, wake)
    db_conn.execute("UPDATE agents_meta SET pid=12345 WHERE id=%s", (agent_id,))
    assert not pending_allocation(db_conn, agent_id, wake)
    db_conn.commit()


def test_only_authorized_actual_admission_can_win(db_conn: psycopg.Connection) -> None:
    agent_id = _row(db_conn)
    wake = insert_inbound_message(db_conn, agent_id, "wake", "user")
    db_conn.commit()
    command = _prepare(db_conn, agent_id, wake)
    with pytest.raises(ResurrectError, match="launch identity"):
        claim_agent_row(agent_id)
    with pytest.raises(ResurrectError, match="committed OS authorization"):
        claim_agent_row(agent_id, resurrect_command_id=command)
    db_conn.execute("SELECT id FROM agents_meta WHERE id=%s FOR UPDATE", (agent_id,))
    authorize_launch(db_conn, agent_id, command, 2)
    db_conn.commit()
    claim_agent_row(agent_id, resurrect_command_id=command)
    winner = db_conn.execute(
        "SELECT pid,runtime_generation FROM agents_meta WHERE id=%s", (agent_id,)
    ).fetchone()
    with pytest.raises(ResurrectError, match="allocation changed"):
        claim_agent_row(agent_id, resurrect_command_id=command)
    assert (
        db_conn.execute(
            "SELECT pid,runtime_generation FROM agents_meta WHERE id=%s", (agent_id,)
        ).fetchone()
        == winner
    )
    db_conn.commit()


@pytest.mark.parametrize("field", ["deadline", "allocation_epoch"])
def test_delayed_attempt_refuses_without_changing_pending_work(
    db_conn: psycopg.Connection, field: str
) -> None:
    agent_id = _row(db_conn)
    wake = insert_inbound_message(db_conn, agent_id, "wake", "user")
    db_conn.commit()
    command = _prepare(db_conn, agent_id, wake)
    db_conn.execute("SELECT id FROM agents_meta WHERE id=%s FOR UPDATE", (agent_id,))
    authorize_launch(db_conn, agent_id, command, 2)
    db_conn.execute(
        "UPDATE inbound_messages SET payload=jsonb_set(payload,%s, "
        "to_jsonb((clock_timestamp()-interval '1 day')::text)) WHERE id=%s",
        (["resurrection_launch", field], command),
    )
    db_conn.commit()
    with pytest.raises(ResurrectError, match="allocation changed or deadline expired"):
        claim_agent_row(agent_id, resurrect_command_id=command)
    assert db_conn.execute(
        "SELECT status FROM inbound_messages WHERE id=%s", (wake,)
    ).fetchone() == ("pending",)
    db_conn.commit()


@pytest.mark.parametrize("key", ["resurrection_launch", "resurrection_launch_attempts"])
def test_external_producer_cannot_authorize_launch(db_conn: psycopg.Connection, key: str) -> None:
    agent_id = _row(db_conn)
    with pytest.raises(ValueError, match="reserved"):
        insert_inbound_message(db_conn, agent_id, "wake", "user", payload={key: {}})
