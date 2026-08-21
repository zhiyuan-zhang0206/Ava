"""`plugins.ava_fleet.task_maintenance.daemon` — task reminders + escalation.

Two cluster-wide passes, gateway-owned:

- `_run_reminders` finds in-progress tasks past their remind_interval_seconds and delivers a
  chat reminder to the owner through the gateway (`_deliver_message` → POST
  /api/agents/{id}/messages), which auto-resurrects a terminated owner. Each
  overdue window gets at most one reminder per backoff period; the counters advance
  only after delivery succeeds.
- `_run_escalate` notifies the parent task's owner when reminder_count reaches the
  escalation threshold.

Delivery is exercised against a stubbed `_deliver_message` (the real one needs a
live gateway; auto-resurrect is covered by the gateway delivery tests). These
tests assert who the daemon delivers to and that the counters advance. No stale
sweep, no automatic cancellation. History is preserved: rows are UPDATEd, never
DELETEd.
"""

from __future__ import annotations

from itertools import count
from typing import Any

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from ava_builtins.plugins.ava_fleet.task_maintenance import daemon
from ava_builtins.plugins.ava_fleet.task_maintenance.daemon import _run_escalate, _run_reminders
from shared.config import settings

_DAY_S = 86400.0


@pytest.fixture
def pool():
    p = ConnectionPool(settings.data_plane.db_url, min_size=1, max_size=2, open=True)
    try:
        yield p
    finally:
        p.close()


@pytest.fixture
def deliver(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, str]]:
    """Stub `_deliver_message`, recording (agent_id, message) per call.

    The real one POSTs to the gateway; here we only assert the daemon's own
    responsibility — which owner it delivers to and that the counters advance."""
    calls: list[tuple[int, str]] = []

    def _fake(agent_id: int, message: str) -> None:
        calls.append((agent_id, message))

    monkeypatch.setattr(daemon, "_deliver_message", _fake)
    return calls


def _make_agent(db: psycopg.Connection, *, status: str = "running") -> int:
    with db.cursor() as cur:
        cur.execute("INSERT INTO agents DEFAULT VALUES RETURNING id")
        aid = int(cur.fetchone()[0])  # type: ignore[index]
        cur.execute(
            "INSERT INTO agents_meta (id, spawner, status) VALUES (%s, 'test', %s)",
            (aid, status),
        )
    db.commit()
    return aid


_TASK_TITLE = count(1)


def _make_task(
    db: psycopg.Connection,
    *,
    status: str = "in_progress",
    owner: int | None = None,
    parent_id: int | None = None,
    updated_s_ago: float = 0.0,
    remind_interval_seconds: int | None = 1800,
    last_reminded_s_ago: float | None = None,
    reminder_count: int = 0,
    priority: str = "P2",
    title: str | None = None,
) -> int:
    # Distinct titles by default: the agent_tasks partial unique index forbids
    # two open/in_progress rows sharing a title, and these tests create many.
    if title is None:
        title = f"t-{next(_TASK_TITLE)}"
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_tasks (title, description, status, owner, created_by, "
            "parent_id, remind_interval_seconds, priority) "
            "VALUES (%s, 'd', %s, %s, 'user', %s, %s, %s) RETURNING id",
            (title, status, owner, parent_id, remind_interval_seconds, priority),
        )
        tid = int(cur.fetchone()[0])  # type: ignore[index]
        cur.execute(
            "UPDATE agent_tasks SET updated_at = now() - make_interval(secs => %s) WHERE id = %s",
            (updated_s_ago, tid),
        )
        if last_reminded_s_ago is not None:
            cur.execute(
                "UPDATE agent_tasks SET last_reminded_at = now() - make_interval(secs => %s) "
                "WHERE id = %s",
                (last_reminded_s_ago, tid),
            )
        if reminder_count:
            cur.execute(
                "UPDATE agent_tasks SET reminder_count = %s WHERE id = %s",
                (reminder_count, tid),
            )
    db.commit()
    return tid


def _task_row(db: psycopg.Connection, tid: int) -> tuple[Any, ...] | None:
    with db.cursor() as cur:
        cur.execute(
            "SELECT status, owner, reminder_count, last_reminded_at FROM agent_tasks WHERE id = %s",
            (tid,),
        )
        return cur.fetchone()


def _open_notices(db: psycopg.Connection, agent_id: int) -> list[tuple[str, str, bool, int]]:
    """Open notices on an agent: (title, priority, require_response, task_id)."""
    db.rollback()  # the daemon committed on its own connection; refresh our view
    with db.cursor() as cur:
        cur.execute(
            "SELECT title, priority, require_response, task_id FROM agent_notices "
            "WHERE agent_id = %s AND resolved_at IS NULL ORDER BY local_id",
            (agent_id,),
        )
        return cur.fetchall()


def _seed_notice(db: psycopg.Connection, agent_id: int) -> None:
    """Give an agent one pre-existing open FYI notice."""
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_notices (agent_id, local_id, title, priority, require_response, blocking) "
            "VALUES (%s, 0, 'pre-existing', 'P2', FALSE, FALSE)",
            (agent_id,),
        )
    db.commit()


class TestRemind:
    def test_reminds_overdue_owner(
        self, pool: ConnectionPool, db_conn: psycopg.Connection, deliver: list[tuple[int, str]]
    ) -> None:
        owner = _make_agent(db_conn)
        tid = _make_task(db_conn, owner=owner, remind_interval_seconds=1800, updated_s_ago=3600)
        assert _run_reminders(pool, 3600.0) == 1
        assert len(deliver) == 1
        delivered_owner, message = deliver[0]
        assert delivered_owner == owner
        assert f"#{tid}" in message
        row = _task_row(db_conn, tid)
        assert row is not None
        _status, _owner, reminder_count, last_reminded_at = row
        assert reminder_count == 1
        assert last_reminded_at is not None

    def test_not_yet_overdue_is_skipped(
        self, pool: ConnectionPool, db_conn: psycopg.Connection, deliver: list[tuple[int, str]]
    ) -> None:
        owner = _make_agent(db_conn)
        _make_task(db_conn, owner=owner, remind_interval_seconds=3600, updated_s_ago=1800)
        assert _run_reminders(pool, 3600.0) == 0
        assert deliver == []

    def test_terminated_owner_is_reminded(
        self, pool: ConnectionPool, db_conn: psycopg.Connection, deliver: list[tuple[int, str]]
    ) -> None:
        dead = _make_agent(db_conn, status="terminated")
        _make_task(db_conn, owner=dead, remind_interval_seconds=1800, updated_s_ago=3600)
        # Terminated owners ARE reminded — the gateway delivery path auto-resurrects
        # them; the daemon's SELECT does not filter on owner status.
        assert _run_reminders(pool, 3600.0) == 1
        assert [c[0] for c in deliver] == [dead]

    def test_delivery_failure_leaves_counters_untouched(
        self,
        pool: ConnectionPool,
        db_conn: psycopg.Connection,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A failed delivery does not bump last_reminded_at / reminder_count, so the
        task is retried on the next sweep."""
        owner = _make_agent(db_conn)
        tid = _make_task(db_conn, owner=owner, remind_interval_seconds=1800, updated_s_ago=3600)

        def _boom(agent_id: int, message: str) -> None:
            raise RuntimeError("gateway down")

        monkeypatch.setattr(daemon, "_deliver_message", _boom)
        assert _run_reminders(pool, 3600.0) == 0
        row = _task_row(db_conn, tid)
        assert row is not None
        _status, _owner, reminder_count, last_reminded_at = row
        assert reminder_count == 0
        assert last_reminded_at is None

    def test_null_remind_interval_is_skipped(
        self, pool: ConnectionPool, db_conn: psycopg.Connection, deliver: list[tuple[int, str]]
    ) -> None:
        owner = _make_agent(db_conn)
        _make_task(db_conn, owner=owner, remind_interval_seconds=None, updated_s_ago=100 * _DAY_S)
        assert _run_reminders(pool, 3600.0) == 0

    def test_open_task_not_reminded(
        self, pool: ConnectionPool, db_conn: psycopg.Connection, deliver: list[tuple[int, str]]
    ) -> None:
        owner = _make_agent(db_conn)
        _make_task(
            db_conn, status="open", owner=owner, remind_interval_seconds=1800, updated_s_ago=3600
        )
        assert _run_reminders(pool, 3600.0) == 0

    def test_backoff_blocks_duplicate_reminder(
        self, pool: ConnectionPool, db_conn: psycopg.Connection, deliver: list[tuple[int, str]]
    ) -> None:
        owner = _make_agent(db_conn)
        _make_task(
            db_conn,
            owner=owner,
            remind_interval_seconds=1800,
            updated_s_ago=7200,
            last_reminded_s_ago=1800,  # reminded 30 min ago, backoff is 1h
        )
        assert _run_reminders(pool, 3600.0) == 0

    def test_backoff_expired_allows_new_reminder(
        self, pool: ConnectionPool, db_conn: psycopg.Connection, deliver: list[tuple[int, str]]
    ) -> None:
        owner = _make_agent(db_conn)
        _make_task(
            db_conn,
            owner=owner,
            remind_interval_seconds=1800,
            updated_s_ago=7200,
            last_reminded_s_ago=7200,  # reminded 2h ago, backoff is 1h
        )
        assert _run_reminders(pool, 3600.0) == 1

    def test_interval_floor_blocks_hourly_nag(
        self, pool: ConnectionPool, db_conn: psycopg.Connection, deliver: list[tuple[int, str]]
    ) -> None:
        """A task whose remind_interval exceeds the backoff floor repeats at its
        own interval: a P3 task (4h) reminded 1.5h ago is NOT nagged again,
        even though the 1h backoff has elapsed (pre-floor behavior would
        remind it every hour once overdue)."""
        owner = _make_agent(db_conn)
        _make_task(
            db_conn,
            owner=owner,
            remind_interval_seconds=14400,
            updated_s_ago=20000,
            last_reminded_s_ago=5400,  # 1.5h ago
        )
        assert _run_reminders(pool, 3600.0) == 0
        assert deliver == []

    def test_interval_floor_allows_repeat_after_full_interval(
        self, pool: ConnectionPool, db_conn: psycopg.Connection, deliver: list[tuple[int, str]]
    ) -> None:
        owner = _make_agent(db_conn)
        _make_task(
            db_conn,
            owner=owner,
            remind_interval_seconds=14400,
            updated_s_ago=20000,
            last_reminded_s_ago=15000,  # 4h+ elapsed — a fresh reminder is due
        )
        assert _run_reminders(pool, 3600.0) == 1

    def test_counter_failure_retries_without_redelivery(
        self,
        pool: ConnectionPool,
        db_conn: psycopg.Connection,
        deliver: list[tuple[int, str]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Same-cause dedup: when the reminder message lands but the counter
        write fails (a DB blip after a 2xx delivery), the next sweep retries
        the counter write WITHOUT re-delivering the message — the owner gets
        one reminder, not a duplicate minutes later."""
        monkeypatch.setattr(daemon, "_pending_counter_writes", {})
        owner = _make_agent(db_conn)
        tid = _make_task(db_conn, owner=owner, remind_interval_seconds=1800, updated_s_ago=3600)

        real = daemon._advance_reminder_counters
        attempts = {"n": 0}

        def _flaky(pool_: ConnectionPool, task_id: int) -> None:
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise psycopg.OperationalError("db blip")
            real(pool_, task_id)

        monkeypatch.setattr(daemon, "_advance_reminder_counters", _flaky)

        # First sweep: the message is delivered, the counter write fails.
        assert _run_reminders(pool, 3600.0) == 0
        assert len(deliver) == 1
        row = _task_row(db_conn, tid)
        assert row is not None
        _status, _owner, reminder_count, last_reminded_at = row
        assert reminder_count == 0
        assert last_reminded_at is None

        # Second sweep: counters advance, no second message is delivered.
        assert _run_reminders(pool, 3600.0) == 0
        assert len(deliver) == 1  # still exactly one reminder delivered
        row = _task_row(db_conn, tid)
        assert row is not None
        _status, _owner, reminder_count, last_reminded_at = row
        assert reminder_count == 1
        assert last_reminded_at is not None

    def test_expired_counter_mark_does_not_suppress_new_reminder(
        self,
        pool: ConnectionPool,
        db_conn: psycopg.Connection,
        deliver: list[tuple[int, str]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The dedup mark is window-bounded: past the window the task is
        reminded again — a new overdue window (the owner updated the task,
        resetting the counters) must not lose its reminder to a stale mark."""
        monkeypatch.setattr(daemon, "_pending_counter_writes", {})

        class _FakeTime:
            now = 0.0

            @staticmethod
            def monotonic() -> float:
                return _FakeTime.now

        monkeypatch.setattr(daemon, "time", _FakeTime)
        owner = _make_agent(db_conn)
        tid = _make_task(db_conn, owner=owner, remind_interval_seconds=1800, updated_s_ago=3600)

        real = daemon._advance_reminder_counters
        attempts = {"n": 0}

        def _flaky(pool_: ConnectionPool, task_id: int) -> None:
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise psycopg.OperationalError("db blip")
            real(pool_, task_id)

        monkeypatch.setattr(daemon, "_advance_reminder_counters", _flaky)

        assert _run_reminders(pool, 3600.0) == 0
        assert len(deliver) == 1

        # Time passes beyond the dedup window, the owner updates the task
        # (counters reset), and the task is overdue again.
        _FakeTime.now = daemon._DELIVER_DEDUP_WINDOW_S + 1.0
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE agent_tasks SET last_reminded_at = NULL, reminder_count = 0, "
                "updated_at = now() - make_interval(secs => 3600) WHERE id = %s",
                (tid,),
            )
        db_conn.commit()

        # The stale mark is expired: a fresh reminder is delivered.
        assert _run_reminders(pool, 3600.0) == 1
        assert len(deliver) == 2


class TestEscalate:
    def test_escalates_at_threshold(
        self, pool: ConnectionPool, db_conn: psycopg.Connection, deliver: list[tuple[int, str]]
    ) -> None:
        parent_owner = _make_agent(db_conn)
        parent = _make_task(db_conn, owner=parent_owner, remind_interval_seconds=None)
        owner = _make_agent(db_conn)
        _make_task(
            db_conn,
            owner=owner,
            parent_id=parent,
            remind_interval_seconds=1800,
            updated_s_ago=7200,
            reminder_count=3,
        )
        assert _run_escalate(pool, 3) == 1
        assert len(deliver) == 1
        delivered_owner, message = deliver[0]
        assert delivered_owner == parent_owner
        assert "3 reminders" in message

    def test_below_threshold_is_skipped(
        self, pool: ConnectionPool, db_conn: psycopg.Connection, deliver: list[tuple[int, str]]
    ) -> None:
        parent_owner = _make_agent(db_conn)
        parent = _make_task(db_conn, owner=parent_owner, remind_interval_seconds=None)
        owner = _make_agent(db_conn)
        _make_task(
            db_conn,
            owner=owner,
            parent_id=parent,
            remind_interval_seconds=1800,
            updated_s_ago=7200,
            reminder_count=2,
        )
        assert _run_escalate(pool, 3) == 0
        assert deliver == []

    def test_above_threshold_only_escalates_once(
        self, pool: ConnectionPool, db_conn: psycopg.Connection, deliver: list[tuple[int, str]]
    ) -> None:
        """reminder_count=5 > threshold=3: escalates only on the exact threshold
        match (reminder_count==3), not on higher values."""
        parent_owner = _make_agent(db_conn)
        parent = _make_task(db_conn, owner=parent_owner, remind_interval_seconds=None)
        owner = _make_agent(db_conn)
        _make_task(
            db_conn,
            owner=owner,
            parent_id=parent,
            remind_interval_seconds=1800,
            updated_s_ago=7200,
            reminder_count=5,
        )
        assert _run_escalate(pool, 3) == 0
        assert deliver == []

    def test_no_parent_no_escalation(
        self, pool: ConnectionPool, db_conn: psycopg.Connection, deliver: list[tuple[int, str]]
    ) -> None:
        owner = _make_agent(db_conn)
        _make_task(
            db_conn,
            owner=owner,
            remind_interval_seconds=1800,
            updated_s_ago=7200,
            reminder_count=3,
        )
        assert _run_escalate(pool, 3) == 0
        assert deliver == []

    def test_user_task_escalates_to_human_queue(
        self, pool: ConnectionPool, db_conn: psycopg.Connection, deliver: list[tuple[int, str]]
    ) -> None:
        """A stalled top-level task whose parent is ownerless (the system root)
        has no delegator to catch it — it escalates to the user as a
        require_response notice on the stalled owner, grouped under the task and
        inheriting its priority. No chat message is delivered."""
        # Ownerless parent stands in for the system root (its owner is NULL).
        root = _make_task(db_conn, owner=None, remind_interval_seconds=None)
        owner = _make_agent(db_conn)
        child = _make_task(
            db_conn,
            owner=owner,
            parent_id=root,
            remind_interval_seconds=1800,
            updated_s_ago=7200,
            reminder_count=3,
            priority="P1",
        )
        assert _run_escalate(pool, 3) == 1
        assert deliver == []  # a notice, not a reminder message
        notices = _open_notices(db_conn, owner)
        assert len(notices) == 1
        title, priority, require_response, task_id = notices[0]
        assert require_response is True
        assert task_id == child  # grouped under the stalled task
        assert priority == "P1"  # inherits the task's priority
        assert "stalled" in title.lower()

    def test_user_escalation_skipped_when_owner_has_open_notice(
        self, pool: ConnectionPool, db_conn: psycopg.Connection, deliver: list[tuple[int, str]]
    ) -> None:
        """When the stalled owner already has an open notice, the user
        escalation is skipped — the human already has that agent flagged and the
        one-open-notice-per-agent invariant holds."""
        root = _make_task(db_conn, owner=None, remind_interval_seconds=None)
        owner = _make_agent(db_conn)
        _make_task(
            db_conn,
            owner=owner,
            parent_id=root,
            remind_interval_seconds=1800,
            updated_s_ago=7200,
            reminder_count=3,
            priority="P1",
        )
        _seed_notice(db_conn, owner)
        assert _run_escalate(pool, 3) == 0
        notices = _open_notices(db_conn, owner)
        assert len(notices) == 1
        assert notices[0][0] == "pre-existing"

    def test_user_escalation_retries_past_threshold(
        self, pool: ConnectionPool, db_conn: psycopg.Connection, deliver: list[tuple[int, str]]
    ) -> None:
        """A user task whose reminder_count has already climbed PAST the threshold
        still escalates — the earlier skip (owner busy) must not permanently miss
        the window. This is the >= gate, not the exact-equality one the parent
        branch keeps."""
        root = _make_task(db_conn, owner=None, remind_interval_seconds=None)
        owner = _make_agent(db_conn)
        child = _make_task(
            db_conn,
            owner=owner,
            parent_id=root,
            remind_interval_seconds=1800,
            updated_s_ago=7200,
            reminder_count=5,  # already past escalate_n=3
            priority="P1",
        )
        assert _run_escalate(pool, 3) == 1
        assert deliver == []
        notices = _open_notices(db_conn, owner)
        assert len(notices) == 1
        assert notices[0][3] == child  # task_id on the escalation notice

    def test_user_escalation_idempotent_once_posted(
        self, pool: ConnectionPool, db_conn: psycopg.Connection, deliver: list[tuple[int, str]]
    ) -> None:
        """Once the escalation notice is posted, a later sweep (still past the
        threshold) sees it open and does not post a second — the notice is the
        idempotency marker."""
        root = _make_task(db_conn, owner=None, remind_interval_seconds=None)
        owner = _make_agent(db_conn)
        _make_task(
            db_conn,
            owner=owner,
            parent_id=root,
            remind_interval_seconds=1800,
            updated_s_ago=7200,
            reminder_count=3,
            priority="P2",
        )
        assert _run_escalate(pool, 3) == 1  # first sweep posts
        assert _run_escalate(pool, 3) == 0  # second sweep: escalation notice still open → skip
        assert len(_open_notices(db_conn, owner)) == 1


def test_healthcheck_respawn_cmd_module_is_importable() -> None:
    """Smoke-test the watchdog respawn chain (audit round 2, P1): the
    healthcheck's `-m <module>` respawn command broke silently when the
    plugins moved into ava_builtins/ — a stale module string never fails an
    import check at deploy time, so the watchdog could never revive the
    daemon after a crash. Pin the actual literal in healthcheck.py to a
    module that resolves, and make sure the pre-move path is gone."""
    import importlib.util
    import re
    from pathlib import Path

    from ava_builtins.plugins.ava_fleet.task_maintenance import healthcheck

    src = Path(healthcheck.__file__).read_text()
    m = re.search(r"\.venv/bin/python -m ([\w.]+)", src)
    assert m, "no `-m <module>` respawn cmd found in healthcheck.py"
    module = m.group(1)
    assert importlib.util.find_spec(module) is not None, (
        f"respawn cmd module {module!r} is not importable — the watchdog "
        "can never revive task-maintenance"
    )
    assert not module.startswith("plugins."), (
        f"respawn cmd {module!r} still uses the pre-ava_builtins path"
    )
