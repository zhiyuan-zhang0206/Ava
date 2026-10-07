"""Escalation acceptance survives competing workers, writers and notice creators."""

from concurrent.futures import ThreadPoolExecutor
from time import monotonic, sleep
from typing import Any

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from ava_builtins.plugins.ava_fleet.task_maintenance import escalation
from ava_builtins.plugins.ava_fleet.task_maintenance.escalation import accept_escalations
from ava_builtins.plugins.ava_fleet.task_maintenance.tests.test_task_maintenance_escalation import (
    _escalated_at,
    _inbound_messages,
    _make_agent,
    _make_task,
    _stalled_subtask,
)
from ava_builtins.plugins.ava_fleet.task_maintenance.tests.test_task_maintenance_escalation import (
    pool as pool,
)


def test_competing_rounds_accept_one_window(
    pool: ConnectionPool, db_conn: psycopg.Connection
) -> None:
    delegator, _, task = _stalled_subtask(db_conn)
    with ThreadPoolExecutor(max_workers=2) as workers:
        rounds = [workers.submit(accept_escalations, pool, 3) for _ in range(2)]
        assert sorted(len(future.result(timeout=10)) for future in rounds) == [0, 1]
    assert len(_inbound_messages(db_conn, delegator)) == 1
    assert _escalated_at(db_conn, task) is not None


@pytest.mark.parametrize("close", [False, True])
def test_locked_parent_change_defers_and_uses_current_state(
    pool: ConnectionPool,
    db_conn: psycopg.Connection,
    close: bool,
) -> None:
    old_delegator, _, task = _stalled_subtask(db_conn)
    new_delegator = _make_agent(db_conn)
    row = db_conn.execute("SELECT parent_id FROM agent_tasks WHERE id=%s", (task,)).fetchone()
    assert row is not None
    parent = row[0]
    db_conn.execute("UPDATE agent_tasks SET owner=%s WHERE id=%s", (new_delegator, parent))
    if close:
        # A parent closes with its child, following the registry close guard.
        db_conn.execute("UPDATE agent_tasks SET status='done' WHERE id=ANY(%s)", ([parent, task],))
    with ThreadPoolExecutor(max_workers=1) as workers:
        future = workers.submit(accept_escalations, pool, 3)
        try:
            assert future.result(timeout=3) == []
        finally:
            db_conn.commit()
    assert _inbound_messages(db_conn, old_delegator) == []
    assert _escalated_at(db_conn, task) is None
    accepted = accept_escalations(pool, 3)
    assert len(accepted) == (0 if close else 1)
    if not close:
        assert accepted[0].recipient == new_delegator
    assert _inbound_messages(db_conn, old_delegator) == []


def test_locked_update_cannot_escalate_old_overdue_window(
    pool: ConnectionPool,
    db_conn: psycopg.Connection,
) -> None:
    delegator, _, task = _stalled_subtask(db_conn)
    db_conn.execute("UPDATE agent_tasks SET reminder_count=0 WHERE id=%s", (task,))
    with ThreadPoolExecutor(max_workers=1) as workers:
        future = workers.submit(accept_escalations, pool, 3)
        try:
            assert future.result(timeout=3) == []
        finally:
            db_conn.commit()
    assert accept_escalations(pool, 3) == []
    assert _inbound_messages(db_conn, delegator) == []
    assert _escalated_at(db_conn, task) is None


def _wait_for_blocker(pool: ConnectionPool, blocker: int) -> None:
    deadline = monotonic() + 5
    while monotonic() < deadline:
        with pool.connection() as conn:
            row = conn.execute(
                "SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE %s=ANY(pg_blocking_pids(pid)))",
                (blocker,),
            ).fetchone()
        if row is not None and row[0]:
            return
        sleep(0.01)
    raise AssertionError("escalation did not wait for the notice creator's transaction")


def test_human_notice_shares_creator_lock(
    pool: ConnectionPool,
    db_conn: psycopg.Connection,
) -> None:
    owner = _make_agent(db_conn)
    # The ownerless root is the existing human-escalation policy boundary.
    root = db_conn.execute(
        "INSERT INTO agent_tasks (title,description,status,created_by,is_root) "
        "VALUES ('root','root','in_progress','user',TRUE) RETURNING id"
    ).fetchone()
    assert root is not None
    task = _make_task(db_conn, owner=owner, parent_id=root[0], reminder_count=3)
    # Model gateway creation's real identity lock and uncommitted notice. The
    # competing daemon must wait before its open check/local-id allocation.
    db_conn.execute("SELECT id FROM agents WHERE id=%s FOR UPDATE", (owner,))
    with ThreadPoolExecutor(max_workers=1) as workers:
        future = workers.submit(accept_escalations, pool, 3)
        try:
            _wait_for_blocker(pool, db_conn.info.backend_pid)
            # Escalation now holds task rows while waiting on the identity.
            # Gateway must still be able to take the FK key-share lock.
            db_conn.execute("SET LOCAL statement_timeout = '2s'")
            db_conn.execute(
                "INSERT INTO agent_notices (agent_id,local_id,task_id,title,priority,require_response,expire_at) "
                "VALUES (%s,0,%s,'concurrent gateway notice','P2',FALSE,now()+interval '1 day')",
                (owner, task),
            )
        finally:
            db_conn.commit()
        assert future.result(timeout=10) == []
    assert db_conn.execute(
        "SELECT count(*) FROM agent_notices WHERE agent_id=%s AND resolved_at IS NULL",
        (owner,),
    ).fetchone() == (1,)


def test_acceptance_failure_rolls_back_entire_batch(
    pool: ConnectionPool,
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first, _, one = _stalled_subtask(db_conn)
    second, _, two = _stalled_subtask(db_conn)
    real = escalation.insert_inbound_message_in_transaction
    attempts = 0

    def fail_second(*args: Any, **kwargs: Any) -> Any:
        nonlocal attempts
        attempts += 1
        if attempts == 2:
            raise psycopg.OperationalError("second digest rejected")
        return real(*args, **kwargs)

    with monkeypatch.context() as injected:
        injected.setattr(escalation, "insert_inbound_message_in_transaction", fail_second)
        with pytest.raises(psycopg.OperationalError):
            accept_escalations(pool, 3)
    assert _inbound_messages(db_conn, first) == []
    assert _inbound_messages(db_conn, second) == []
    assert _escalated_at(db_conn, one) is None and _escalated_at(db_conn, two) is None
    assert len(accept_escalations(pool, 3)) == 2
    assert accept_escalations(pool, 3) == []
