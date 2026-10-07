"""`plugins.ava_fleet.task_maintenance.daemon` — task reminders + escalation.

Two cluster-wide passes, gateway-owned:

- `_run_reminders` finds in-progress tasks past their remind_interval_seconds and delivers one
  chat digest per owner through a direct inbound insert. Terminated owners keep
  their inbox row without being revived. Each overdue window gets at most one
  reminder per backoff period; the counters advance only after delivery succeeds.
- `_run_escalate` notifies the parent task's owner when reminder_count reaches the
  escalation threshold.

Delivery is normally exercised against a stubbed `deliver_message`; the
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
from ava_builtins.plugins.ava_fleet.task_maintenance.daemon import _run_escalate
from base import telemetry
from base.config import settings
from base.db import Database
from base.events.live.bus import EventBus

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
    """Stub `deliver_message`, recording (agent_id, message) per call.

    The real one writes an inbound row; here we only assert the daemon's own
    responsibility — digest recipients, content, and counter updates."""
    calls: list[tuple[int, str]] = []

    def _fake(
        pool_: ConnectionPool,
        _db: object,
        _bus: object,
        agent_id: int,
        message: str,
        **_kwargs: object,
    ) -> None:
        calls.append((agent_id, message))

    monkeypatch.setattr(daemon, "deliver_message", _fake)

    def _accepted(_db: object, _bus: object, owner: int, _inbound_id: int, content: str) -> None:
        calls.append((owner, content))

    monkeypatch.setattr(daemon, "announce_reminder", _accepted)
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
    # two in_progress rows sharing a title, and these tests create many.
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
            "INSERT INTO agent_notices (agent_id, local_id, title, priority, require_response, blocking, expire_at) "
            "VALUES (%s, 0, 'pre-existing', 'P2', FALSE, FALSE, now() + interval '1 day')",
            (agent_id,),
        )
    db.commit()


class TestEscalate:
    def test_delegator_receives_one_digest_for_all_stalled_subtasks(
        self,
        pool: ConnectionPool,
        db_conn: psycopg.Connection,
        deliver: list[tuple[int, str]],
        emitted_events: list[tuple[str, str, dict[str, Any]]],
        database: Database,
        event_bus: EventBus,
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

        assert _run_escalate(pool, database, event_bus, 3) == 1
        assert len(deliver) == 1
        delivered_owner, message = deliver[0]
        assert delivered_owner == delegator
        assert "Stalled subtasks — owner(s) unresponsive after repeated reminders:" in message
        assert all(f"#{task_id}" in message for task_id in task_ids)
        # _ESCALATE_SQL sorts nothing: row order is unspecified, and neither the
        # digest message (checked membership-wise above) nor the telemetry makes
        # an ordering claim — assert the id set, not the SQL's incidental row
        # order. Exact-list equality here was a shard7 flake (task #2199).
        assert emitted_events == [
            (
                "telemetry",
                "task_escalation",
                {
                    "attributes": {
                        "owner_id": delegator,
                        "task_count": 2,
                        "task_ids": sorted(task_ids),
                        "leg": "delegator",
                    },
                    "agent_id": delegator,
                    "source": "system",
                },
            )
        ]

    def test_escalates_at_threshold(
        self,
        pool: ConnectionPool,
        db_conn: psycopg.Connection,
        deliver: list[tuple[int, str]],
        database: Database,
        event_bus: EventBus,
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
        assert _run_escalate(pool, database, event_bus, 3) == 1
        assert len(deliver) == 1
        delivered_owner, message = deliver[0]
        assert delivered_owner == parent_owner
        assert "3 reminders" in message

    def test_below_threshold_is_skipped(
        self,
        pool: ConnectionPool,
        db_conn: psycopg.Connection,
        deliver: list[tuple[int, str]],
        database: Database,
        event_bus: EventBus,
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
        assert _run_escalate(pool, database, event_bus, 3) == 0
        assert deliver == []

    def test_above_threshold_escalates_once_per_window(
        self,
        pool: ConnectionPool,
        db_conn: psycopg.Connection,
        deliver: list[tuple[int, str]],
        database: Database,
        event_bus: EventBus,
    ) -> None:
        """reminder_count=5 > threshold=3 with no marker: still one digest —
        the user leg's >= rule, not the old exact equality."""
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
        assert _run_escalate(pool, database, event_bus, 3) == 1
        assert len(deliver) == 1

    def test_no_parent_no_escalation(
        self,
        pool: ConnectionPool,
        db_conn: psycopg.Connection,
        deliver: list[tuple[int, str]],
        database: Database,
        event_bus: EventBus,
    ) -> None:
        owner = _make_agent(db_conn)
        _make_task(
            db_conn,
            owner=owner,
            remind_interval_seconds=1800,
            updated_s_ago=7200,
            reminder_count=3,
        )
        assert _run_escalate(pool, database, event_bus, 3) == 0
        assert deliver == []

    def test_user_task_escalates_to_human_queue(
        self,
        pool: ConnectionPool,
        db_conn: psycopg.Connection,
        deliver: list[tuple[int, str]],
        emitted_events: list[tuple[str, str, dict[str, Any]]],
        database: Database,
        event_bus: EventBus,
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
        assert _run_escalate(pool, database, event_bus, 3) == 1
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
        self,
        pool: ConnectionPool,
        db_conn: psycopg.Connection,
        deliver: list[tuple[int, str]],
        database: Database,
        event_bus: EventBus,
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
        assert _run_escalate(pool, database, event_bus, 3) == 0
        notices = _open_notices(db_conn, owner)
        assert len(notices) == 1
        assert notices[0][0] == "pre-existing"

    def test_user_escalation_retries_past_threshold(
        self,
        pool: ConnectionPool,
        db_conn: psycopg.Connection,
        deliver: list[tuple[int, str]],
        database: Database,
        event_bus: EventBus,
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
        assert _run_escalate(pool, database, event_bus, 3) == 1
        assert deliver == []
        notices = _open_notices(db_conn, owner)
        assert len(notices) == 1
        assert notices[0][3] == child  # task_id on the escalation notice

    def test_user_escalation_idempotent_once_posted(
        self,
        pool: ConnectionPool,
        db_conn: psycopg.Connection,
        deliver: list[tuple[int, str]],
        database: Database,
        event_bus: EventBus,
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
        assert _run_escalate(pool, database, event_bus, 3) == 1  # first sweep posts
        assert (
            _run_escalate(pool, database, event_bus, 3) == 0
        )  # second sweep: escalation notice still open → skip
        assert len(_open_notices(db_conn, owner)) == 1


def test_service_command_module_is_importable() -> None:
    """The canonical root service command resolves after plugin relocation."""
    import importlib.util

    from ava_builtins.plugins.ava_fleet.services import services

    service = next(item for item in services() if item.session == "task-maintenance")
    module = service.cmd.split(" -m ", 1)[1]
    assert importlib.util.find_spec(module) is not None
    assert module.startswith("ava_builtins.plugins.")
