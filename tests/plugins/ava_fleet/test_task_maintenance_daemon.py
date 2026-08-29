"""`plugins.ava_fleet.task_maintenance.daemon` — task reminders + escalation.

Two cluster-wide passes, gateway-owned:

- `_run_reminders` finds in-progress tasks past their remind_interval_seconds and delivers one
  chat digest per owner through a direct inbound insert. Terminated owners keep
  their inbox row without being revived. Each overdue window gets at most one
  reminder per backoff period; the counters advance only after delivery succeeds.
- `_run_escalate` notifies the parent task's owner when reminder_count reaches the
  escalation threshold.

Delivery is normally exercised against a stubbed `_deliver_message`; the
terminated-owner test keeps the direct write real. These tests assert digest
recipients, message contents, counters, telemetry, and no-resurrect delivery. No
stale sweep, no automatic cancellation. History is preserved: rows are UPDATEd,
never DELETEd.
"""

from __future__ import annotations

from itertools import count
from typing import Any

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from ava_builtins.plugins.ava_fleet.task_maintenance import daemon
from ava_builtins.plugins.ava_fleet.task_maintenance.daemon import _run_escalate, _run_reminders
from shared import telemetry
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

    The real one writes an inbound row; here we only assert the daemon's own
    responsibility — digest recipients, content, and counter updates."""
    calls: list[tuple[int, str]] = []

    def _fake(pool_: ConnectionPool, agent_id: int, message: str) -> None:
        calls.append((agent_id, message))

    monkeypatch.setattr(daemon, "_deliver_message", _fake)
    return calls


@pytest.fixture
def emitted_events(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, dict[str, Any]]]:
    """Capture maintenance telemetry without starting an event pipeline."""
    events: list[tuple[str, str, dict[str, Any]]] = []

    def _capture(category: str, event_name: str, **kwargs: Any) -> None:
        events.append((category, event_name, kwargs))

    monkeypatch.setattr(telemetry, "emit", _capture)
    return events


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


def _inbound_messages(db: psycopg.Connection, agent_id: int) -> list[tuple[str, str, str]]:
    """Inbound rows for an agent: (content, kind, source)."""
    db.rollback()  # the daemon committed on its own connection; refresh our view
    with db.cursor() as cur:
        cur.execute(
            "SELECT content, kind, source FROM inbound_messages WHERE agent_id = %s ORDER BY id",
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
    def test_delivery_publishes_agent_updated(
        self,
        pool: ConnectionPool,
        db_conn: psycopg.Connection,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        owner = _make_agent(db_conn)
        published: list[tuple[int, int]] = []

        def _capture_publish(conn: psycopg.Connection, agent_id: int) -> None:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*) FROM inbound_messages WHERE agent_id = %s", (agent_id,)
                )
                row = cur.fetchone()
            assert row is not None
            published.append((agent_id, int(row[0])))

        monkeypatch.setattr(daemon, "publish_agent_updated_sync", _capture_publish)

        daemon._deliver_message(pool, owner, "reminder")

        # The publisher sees the inbound before this connection commits, proving
        # the daemon passed its delivery transaction connection rather than
        # opening a second connection after the insert.
        assert published == [(owner, 1)]
        assert _inbound_messages(db_conn, owner) == [("reminder", "system_note", "system")]

    def test_single_overdue_task_delivers_single_task_digest(
        self,
        pool: ConnectionPool,
        db_conn: psycopg.Connection,
        deliver: list[tuple[int, str]],
        emitted_events: list[tuple[str, str, dict[str, Any]]],
    ) -> None:
        owner = _make_agent(db_conn)
        tid = _make_task(db_conn, owner=owner, remind_interval_seconds=1800, updated_s_ago=3600)
        assert _run_reminders(pool, 3600.0) == 1
        assert len(deliver) == 1
        delivered_owner, message = deliver[0]
        assert delivered_owner == owner
        assert "Task reminders — you have 1 overdue task(s)." in message
        assert "report your current status and advance the next step" in message
        assert f"#{tid}" in message
        assert emitted_events == [
            (
                "telemetry",
                "task_reminder_digest",
                {
                    "agent_id": owner,
                    "source": "system",
                    "attributes": {"owner_id": owner, "task_count": 1, "task_ids": [tid]},
                },
            )
        ]
        row = _task_row(db_conn, tid)
        assert row is not None
        _status, _owner, reminder_count, last_reminded_at = row
        assert reminder_count == 1
        assert last_reminded_at is not None

    def test_owner_receives_one_digest_for_all_overdue_tasks(
        self, pool: ConnectionPool, db_conn: psycopg.Connection, deliver: list[tuple[int, str]]
    ) -> None:
        """A per-task send would deliver three messages and leave this digest absent."""
        owner = _make_agent(db_conn)
        task_ids = [
            _make_task(
                db_conn,
                owner=owner,
                title=f"overdue-{number}",
                remind_interval_seconds=1800,
                updated_s_ago=3600,
            )
            for number in range(3)
        ]

        assert _run_reminders(pool, 3600.0) == 1
        assert len(deliver) == 1
        delivered_owner, message = deliver[0]
        assert delivered_owner == owner
        assert "Task reminders — you have 3 overdue task(s)." in message
        assert all(f"#{task_id}" in message for task_id in task_ids)
        for task_id in task_ids:
            row = _task_row(db_conn, task_id)
            assert row is not None
            assert row[2] == 1
            assert row[3] is not None

    def test_digest_lists_tasks_priority_first(
        self, pool: ConnectionPool, db_conn: psycopg.Connection, deliver: list[tuple[int, str]]
    ) -> None:
        """Overdue tasks in one digest are ordered P0 first (user ruling
        2026-08-29: the reminder orders by priority so the highest-stakes
        task leads the list), regardless of creation order."""
        owner = _make_agent(db_conn)
        p2 = _make_task(
            db_conn,
            owner=owner,
            title="low",
            priority="P2",
            remind_interval_seconds=1800,
            updated_s_ago=3600,
        )
        p0 = _make_task(
            db_conn,
            owner=owner,
            title="top",
            priority="P0",
            remind_interval_seconds=1800,
            updated_s_ago=3600,
        )
        p1 = _make_task(
            db_conn,
            owner=owner,
            title="mid",
            priority="P1",
            remind_interval_seconds=1800,
            updated_s_ago=3600,
        )

        assert _run_reminders(pool, 3600.0) == 1
        assert len(deliver) == 1
        _delivered_owner, message = deliver[0]
        ids_in_order = [int(line.split(" ")[1].lstrip("#")) for line in message.splitlines()[1:]]
        assert ids_in_order == [p0, p1, p2]
        # The imperative instruction rides each line.
        assert "report your current status and advance the next step" in message
        assert "raise the reminder interval to wait explicitly" in message
        # The reminder interval stays on the line.
        assert "reminder interval: 30min" in message

    def test_owners_receive_separate_digests(
        self, pool: ConnectionPool, db_conn: psycopg.Connection, deliver: list[tuple[int, str]]
    ) -> None:
        """Grouping all overdue tasks together would leak one owner's work to another."""
        first_owner = _make_agent(db_conn)
        second_owner = _make_agent(db_conn)
        _make_task(db_conn, owner=first_owner, remind_interval_seconds=1800, updated_s_ago=3600)
        _make_task(db_conn, owner=first_owner, remind_interval_seconds=1800, updated_s_ago=3600)
        _make_task(db_conn, owner=second_owner, remind_interval_seconds=1800, updated_s_ago=3600)

        assert _run_reminders(pool, 3600.0) == 2
        messages_by_owner = dict(deliver)
        assert set(messages_by_owner) == {first_owner, second_owner}
        assert "2 overdue task(s)" in messages_by_owner[first_owner]
        assert "1 overdue task(s)" in messages_by_owner[second_owner]

    def test_not_yet_overdue_is_skipped(
        self, pool: ConnectionPool, db_conn: psycopg.Connection, deliver: list[tuple[int, str]]
    ) -> None:
        owner = _make_agent(db_conn)
        _make_task(db_conn, owner=owner, remind_interval_seconds=3600, updated_s_ago=1800)
        assert _run_reminders(pool, 3600.0) == 0
        assert deliver == []

    def test_terminated_owner_receives_inbound_without_resurrection(
        self, pool: ConnectionPool, db_conn: psycopg.Connection
    ) -> None:
        dead = _make_agent(db_conn, status="terminated")
        task_id = _make_task(db_conn, owner=dead, remind_interval_seconds=1800, updated_s_ago=3600)
        assert _run_reminders(pool, 3600.0) == 1
        inbounds = _inbound_messages(db_conn, dead)
        assert len(inbounds) == 1
        content, kind, source = inbounds[0]
        assert f"#{task_id}" in content
        # A reminder is a task system notification: delivered as a system-note
        # inbound (NoteTag 'task'), never as peer chat (user ruling 2026-08-27).
        assert kind == "system_note"
        assert source == "system"
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT payload FROM inbound_messages WHERE agent_id = %s AND kind = 'system_note'",
                (dead,),
            )
            payload = cur.fetchone()
            assert payload is not None and payload[0] == {"note_tag": "task"}
        with db_conn.cursor() as cur:
            cur.execute("SELECT status FROM agents_meta WHERE id = %s", (dead,))
            assert cur.fetchone() == ("terminated",)

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

        def _boom(pool_: ConnectionPool, agent_id: int, message: str) -> None:
            raise RuntimeError("inbound insert failed")

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
        task_ids = [
            _make_task(db_conn, owner=owner, remind_interval_seconds=1800, updated_s_ago=3600)
            for _ in range(2)
        ]

        real = daemon._advance_reminder_counters
        attempts = {"n": 0}

        def _flaky(pool_: ConnectionPool, task_id: int) -> None:
            if task_id == task_ids[0] and attempts["n"] == 0:
                attempts["n"] += 1
                raise psycopg.OperationalError("db blip")
            real(pool_, task_id)

        monkeypatch.setattr(daemon, "_advance_reminder_counters", _flaky)

        # First sweep: one digest lands; one task counter fails while the
        # other succeeds. The next sweep must only retry the failed counter.
        _run_reminders(pool, 3600.0)
        assert len(deliver) == 1
        failed_row = _task_row(db_conn, task_ids[0])
        advanced_row = _task_row(db_conn, task_ids[1])
        assert failed_row is not None
        assert advanced_row is not None
        assert failed_row[2:] == (0, None)
        assert advanced_row[2] == 1
        assert advanced_row[3] is not None

        # Second sweep: the failed counter advances, with no second digest.
        assert _run_reminders(pool, 3600.0) == 0
        assert len(deliver) == 1
        for task_id in task_ids:
            row = _task_row(db_conn, task_id)
            assert row is not None
            assert row[2] == 1
            assert row[3] is not None

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
    def test_delegator_receives_one_digest_for_all_stalled_subtasks(
        self,
        pool: ConnectionPool,
        db_conn: psycopg.Connection,
        deliver: list[tuple[int, str]],
        emitted_events: list[tuple[str, str, dict[str, Any]]],
    ) -> None:
        """Per-subtask escalation would produce two chats instead of one digest."""
        delegator = _make_agent(db_conn)
        parent = _make_task(db_conn, owner=delegator, remind_interval_seconds=None)
        first_owner = _make_agent(db_conn)
        second_owner = _make_agent(db_conn)
        task_ids = [
            _make_task(
                db_conn,
                owner=owner,
                parent_id=parent,
                remind_interval_seconds=1800,
                updated_s_ago=7200,
                reminder_count=3,
            )
            for owner in (first_owner, second_owner)
        ]

        assert _run_escalate(pool, 3) == 1
        assert len(deliver) == 1
        delivered_owner, message = deliver[0]
        assert delivered_owner == delegator
        assert "Stalled subtasks — owner(s) unresponsive after repeated reminders:" in message
        assert all(f"#{task_id}" in message for task_id in task_ids)
        assert emitted_events == [
            (
                "telemetry",
                "task_escalation",
                {
                    "attributes": {
                        "owner_id": delegator,
                        "task_count": 2,
                        "task_ids": task_ids,
                        "leg": "delegator",
                    },
                    "agent_id": delegator,
                    "source": "system",
                },
            )
        ]

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
        self,
        pool: ConnectionPool,
        db_conn: psycopg.Connection,
        deliver: list[tuple[int, str]],
        emitted_events: list[tuple[str, str, dict[str, Any]]],
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
        assert emitted_events == [
            (
                "telemetry",
                "task_escalation",
                {
                    "attributes": {
                        "owner_id": owner,
                        "task_count": 1,
                        "task_ids": [child],
                        "leg": "user",
                    },
                    "agent_id": owner,
                    "source": "system",
                },
            )
        ]

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
