"""`plugins.ava_fleet.task_registry.create` — the per-priority default reminder interval.

A new task reminds its owner after a silence window that scales with its
priority: P0 30m / P1 1h / P2 2h / P3 4h. `create()` defaults
`remind_interval_seconds` to the priority's window; an explicit value
(positive, <= 24h) always wins. Reminders cannot be disabled — an explicit
`None` falls back to the priority default. Persisted via `ava.DB`.
"""

from __future__ import annotations

import inspect
import re
from collections.abc import Iterator
from unittest.mock import patch

import psycopg
import pytest

from ava_builtins.plugins.ava_fleet import task_registry
from ava_builtins.plugins.ava_fleet.tests.task_registry.notes import record_notes
from tests.fixtures.pin_agent import pin_agent


def _seed_agent(db: psycopg.Connection, *, status: str = "running", spawner: str = "test") -> int:
    with db.cursor() as cur:
        cur.execute("INSERT INTO agents DEFAULT VALUES RETURNING id")
        aid = cur.fetchone()[0]  # type: ignore[index]
        cur.execute(
            "INSERT INTO agents_meta (id, spawner, status) VALUES (%s, %s, %s)",
            (aid, spawner, status),  # pyright: ignore[reportUnknownArgumentType]
        )
    db.commit()
    return aid


def _ignore_system_note(_agent_id: int, _content: str, **_kwargs: object) -> int:
    """Keep task-registry tests on the database-side behavior under test."""
    return 0


@pytest.fixture(autouse=True)
def root_task_id(db_conn: psycopg.Connection) -> Iterator[int]:
    """Seed the system root task (is_root=TRUE, unowned) before each test and
    yield its id. create() requires an explicit parent, so tests anchor their
    tasks under this root (parent=root_task_id); the root itself stays
    parentless."""
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_tasks (title, description, status, created_by, is_root) "
            "VALUES ('Root', 'root', 'in_progress', 'system', TRUE) RETURNING id"
        )
        rid = cur.fetchone()[0]  # type: ignore[index]
    db_conn.commit()
    yield rid


def _persisted_remind_interval_seconds(db: psycopg.Connection, task_id: int) -> int | None:
    with db.cursor() as cur:
        cur.execute("SELECT remind_interval_seconds FROM agent_tasks WHERE id = %s", (task_id,))
        row = cur.fetchone()
    assert row is not None
    return row[0]


def _persisted_priority(db: psycopg.Connection, task_id: int) -> str:
    with db.cursor() as cur:
        cur.execute("SELECT priority FROM agent_tasks WHERE id = %s", (task_id,))
        row = cur.fetchone()
    assert row is not None
    return row[0]


def test_default_remind_interval_is_none_sentinel() -> None:
    """Not-passed and None mean the same thing: resolve against priority, so
    the per-priority default applies at create time."""
    assert (
        inspect.signature(task_registry.create).parameters["remind_interval_seconds"].default
        is None
    )


def test_create_parent_is_required() -> None:
    """`parent` is a required keyword-only parameter with no default — a task
    can never be created without naming the task it descends from."""
    sig = inspect.signature(task_registry.create)
    params = sig.parameters
    assert list(params.keys()) == [
        "title",
        "description",
        "parent",
        "remind_interval_seconds",
        "owner",
        "priority",
    ]
    assert params["description"].default is inspect.Parameter.empty
    assert params["parent"].default is inspect.Parameter.empty
    assert params["parent"].kind is inspect.Parameter.KEYWORD_ONLY


@pytest.mark.parametrize(
    ("priority", "expected"),
    [("P0", 1800), ("P1", 3600), ("P2", 7200), ("P3", 14400)],
)
def test_create_defaults_per_priority(
    db_conn: psycopg.Connection, priority: str, expected: int, root_task_id: int
) -> None:
    """No explicit interval → the priority's default window (P0 30m .. P3 4h)."""
    agent_id = _seed_agent(db_conn)
    pin_agent(agent_id)
    task = task_registry.create("title", "detail", priority=priority, parent=root_task_id)
    assert task.remind_interval_seconds == expected
    assert _persisted_remind_interval_seconds(db_conn, task.id) == expected
    assert _persisted_priority(db_conn, task.id) == priority


def test_create_default_priority_is_p2_with_2h_interval(
    db_conn: psycopg.Connection, root_task_id: int
) -> None:
    agent_id = _seed_agent(db_conn)
    pin_agent(agent_id)
    task = task_registry.create("title", "detail", parent=root_task_id)
    assert task.remind_interval_seconds == 7200
    assert _persisted_remind_interval_seconds(db_conn, task.id) == 7200


def test_create_explicit_interval_beats_priority_default(
    db_conn: psycopg.Connection, root_task_id: int
) -> None:
    """An explicit interval wins over the priority's default (user override)."""
    agent_id = _seed_agent(db_conn)
    pin_agent(agent_id)
    task = task_registry.create(
        "title", "detail", priority="P3", remind_interval_seconds=1800, parent=root_task_id
    )
    assert task.remind_interval_seconds == 1800
    assert _persisted_remind_interval_seconds(db_conn, task.id) == 1800


def test_create_honours_explicit_value(db_conn: psycopg.Connection, root_task_id: int) -> None:
    agent_id = _seed_agent(db_conn)
    pin_agent(agent_id)
    task = task_registry.create(
        "title", "detail", remind_interval_seconds=3600, parent=root_task_id
    )
    assert task.remind_interval_seconds == 3600
    assert _persisted_remind_interval_seconds(db_conn, task.id) == 3600


def test_create_none_falls_back_to_default(db_conn: psycopg.Connection, root_task_id: int) -> None:
    """Reminders cannot be disabled: create(remind_interval_seconds=None) uses the
    priority default rather than writing NULL."""
    agent_id = _seed_agent(db_conn)
    pin_agent(agent_id)
    task = task_registry.create(
        "title", "detail", remind_interval_seconds=None, parent=root_task_id
    )
    assert task.remind_interval_seconds == 7200  # P2 default -> 2h
    assert _persisted_remind_interval_seconds(db_conn, task.id) == 7200


def test_create_rejects_non_positive_interval(
    db_conn: psycopg.Connection, root_task_id: int
) -> None:
    agent_id = _seed_agent(db_conn)
    pin_agent(agent_id)
    with pytest.raises(ValueError, match="positive number of seconds"):
        task_registry.create("title", "detail", remind_interval_seconds=0, parent=root_task_id)


def test_create_rejects_interval_over_24h(db_conn: psycopg.Connection, root_task_id: int) -> None:
    agent_id = _seed_agent(db_conn)
    pin_agent(agent_id)
    with pytest.raises(ValueError, match="cannot be disabled"):
        task_registry.create("title", "detail", remind_interval_seconds=86401, parent=root_task_id)


def test_create_honours_24h_boundary(db_conn: psycopg.Connection, root_task_id: int) -> None:
    """Exactly 24h (86400s) is the largest accepted interval."""
    agent_id = _seed_agent(db_conn)
    pin_agent(agent_id)
    task = task_registry.create(
        "title", "detail", remind_interval_seconds=86400, parent=root_task_id
    )
    assert task.remind_interval_seconds == 86400


def test_update_changes_remind_interval_seconds(
    db_conn: psycopg.Connection, root_task_id: int
) -> None:
    agent_id = _seed_agent(db_conn)
    pin_agent(agent_id)
    task = task_registry.create("title", "detail", parent=root_task_id)
    assert task.remind_interval_seconds == 7200  # P2 default -> 2h
    task_registry.update(task.id, remind_interval_seconds=1800)
    assert _persisted_remind_interval_seconds(db_conn, task.id) == 1800


def test_update_none_remind_interval_is_noop(
    db_conn: psycopg.Connection, root_task_id: int
) -> None:
    """remind_interval_seconds=None means "no change" (reminders cannot be disabled),
    not "write NULL"; alongside a real change it leaves the interval intact."""
    agent_id = _seed_agent(db_conn)
    pin_agent(agent_id)
    task = task_registry.create("title", "detail", parent=root_task_id)
    task_registry.update(task.id, status="in_progress", remind_interval_seconds=None)
    assert _persisted_remind_interval_seconds(db_conn, task.id) == 7200  # P2 default


def test_update_rejects_interval_over_24h(db_conn: psycopg.Connection, root_task_id: int) -> None:
    agent_id = _seed_agent(db_conn)
    pin_agent(agent_id)
    task = task_registry.create("title", "detail", parent=root_task_id)
    with pytest.raises(ValueError, match="cannot be disabled"):
        task_registry.update(task.id, remind_interval_seconds=86401)


def test_update_resets_reminder_count(db_conn: psycopg.Connection, root_task_id: int) -> None:
    agent_id = _seed_agent(db_conn)
    pin_agent(agent_id)
    task = task_registry.create("title", "detail", parent=root_task_id)
    # Simulate some prior reminders and an escalation
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE agent_tasks SET reminder_count = 3, last_reminded_at = now(), "
            "escalated_at = now() WHERE id = %s",
            (task.id,),
        )
    db_conn.commit()
    # An update resets the counters and the escalation marker
    task_registry.update(task.id, results="progress")
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT reminder_count, last_reminded_at, escalated_at FROM agent_tasks WHERE id = %s",
            (task.id,),
        )
        row = cur.fetchone()
    assert row == (0, None, None)


def test_update_description(db_conn: psycopg.Connection, root_task_id: int) -> None:
    agent_id = _seed_agent(db_conn)
    pin_agent(agent_id)
    task = task_registry.create("title", "detail", parent=root_task_id)
    task_registry.update(task.id, description="revised detail")
    assert task_registry.get(task.id).description == "revised detail"


def test_update_nothing_raises(db_conn: psycopg.Connection, root_task_id: int) -> None:
    agent_id = _seed_agent(db_conn)
    pin_agent(agent_id)
    task = task_registry.create("title", "detail", parent=root_task_id)
    with pytest.raises(ValueError, match="at least one"):
        task_registry.update(task.id)


@pytest.mark.parametrize("closing_status", ["done", "cancelled"])
def test_update_rejects_closing_parent_with_in_progress_child(
    db_conn: psycopg.Connection, closing_status: str, root_task_id: int
) -> None:
    """A child is born in_progress, so closing a parent while any child is
    active is rejected (post-2026-08-29 the 'open' status no longer exists —
    a task starts in_progress)."""
    agent_id = _seed_agent(db_conn)
    pin_agent(agent_id)
    parent = task_registry.create(f"parent-{closing_status}", "detail", parent=root_task_id)
    child = task_registry.create(f"active-child-{closing_status}", "detail", parent=parent.id)
    message = (
        f"task {parent.id} has 1 in_progress child tasks (e.g. #{child.id}) — "
        "close or cancel them first"
    )

    with pytest.raises(ValueError, match=re.escape(message)):
        task_registry.update(parent.id, status=closing_status)

    assert task_registry.get(parent.id).status == "in_progress"


def test_update_closes_parent_when_all_children_are_closed(
    db_conn: psycopg.Connection, root_task_id: int
) -> None:
    agent_id = _seed_agent(db_conn)
    pin_agent(agent_id)
    parent = task_registry.create("parent-with-closed-children", "detail", parent=root_task_id)
    done_child = task_registry.create("done-child", "detail", parent=parent.id)
    cancelled_child = task_registry.create("cancelled-child", "detail", parent=parent.id)
    task_registry.update(done_child.id, status="done")
    task_registry.update(cancelled_child.id, status="cancelled")

    task_registry.update(parent.id, status="done")

    assert task_registry.get(parent.id).status == "done"


@pytest.mark.parametrize("closing_status", ["done", "cancelled"])
def test_update_closes_parent_without_children(
    db_conn: psycopg.Connection, closing_status: str, root_task_id: int
) -> None:
    agent_id = _seed_agent(db_conn)
    pin_agent(agent_id)
    task = task_registry.create(
        f"parent-without-children-{closing_status}", "detail", parent=root_task_id
    )

    task_registry.update(task.id, status=closing_status)

    assert task_registry.get(task.id).status == closing_status


def test_update_starts_parent_with_in_progress_child(
    db_conn: psycopg.Connection, root_task_id: int
) -> None:
    agent_id = _seed_agent(db_conn)
    pin_agent(agent_id)
    parent = task_registry.create("parent-to-start", "detail", parent=root_task_id)
    task_registry.create("active-child-of-started-parent", "detail", parent=parent.id)

    task_registry.update(parent.id, status="in_progress")

    assert task_registry.get(parent.id).status == "in_progress"


def test_update_title_and_note_on_parent_with_in_progress_child(
    db_conn: psycopg.Connection, root_task_id: int
) -> None:
    agent_id = _seed_agent(db_conn)
    pin_agent(agent_id)
    parent = task_registry.create("parent-to-edit", "detail", parent=root_task_id)
    task_registry.create("active-child-of-edited-parent", "detail", parent=parent.id)

    task_registry.update(parent.id, title="edited-parent", note="still active")

    updated = task_registry.get(parent.id)
    assert updated.status == "in_progress"
    assert updated.title == "edited-parent"
    assert updated.results is not None
    assert updated.results.splitlines()[-1].endswith("still active")


def test_log_appends_timestamped_lines(db_conn: psycopg.Connection, root_task_id: int) -> None:
    agent_id = _seed_agent(db_conn)
    pin_agent(agent_id)
    task = task_registry.create("title", "detail", parent=root_task_id)
    task_registry.log(task.id, "first note")
    task_registry.log(task.id, "second note")
    results = task_registry.get(task.id).results
    assert results is not None
    lines = results.splitlines()
    assert len(lines) == 2
    assert lines[0].endswith("first note")
    assert lines[1].endswith("second note")
    for line in lines:
        assert re.match(r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] ", line)


def test_log_stamps_in_the_cluster_timezone(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch, root_task_id: int
) -> None:
    """A task note is stamped in `settings.general.timezone`, not the writing
    machine's local timezone. A fleet spans machines and one task's notes are
    appended by several of them, so host-local stamps put unmarked, mutually
    inconsistent wall clocks in one column of text."""
    from base.config import settings

    agent_id = _seed_agent(db_conn)
    pin_agent(agent_id)
    stamps: dict[str, str] = {}
    for tz in ("Asia/Shanghai", "Pacific/Honolulu"):
        monkeypatch.setattr(settings.general, "timezone", tz)
        task = task_registry.create(f"stamped in {tz}", "detail", parent=root_task_id)
        task_registry.log(task.id, "note")
        results = task_registry.get(task.id).results
        assert results is not None
        stamps[tz] = results.split("]")[0]
    # Honolulu is UTC-10 year-round, Shanghai UTC+8: 18 hours apart, so two
    # stamps taken seconds apart cannot agree unless the setting is read.
    assert stamps["Asia/Shanghai"] != stamps["Pacific/Honolulu"]


def test_log_preserves_replaced_results(db_conn: psycopg.Connection, root_task_id: int) -> None:
    agent_id = _seed_agent(db_conn)
    pin_agent(agent_id)
    task = task_registry.create("title", "detail", parent=root_task_id)
    task_registry.update(task.id, results="prior log without newline")
    task_registry.log(task.id, "appended")
    results = task_registry.get(task.id).results
    assert results is not None
    lines = results.splitlines()
    assert lines[0] == "prior log without newline"
    assert lines[1].endswith("appended")


def test_log_resets_reminder_count(db_conn: psycopg.Connection, root_task_id: int) -> None:
    agent_id = _seed_agent(db_conn)
    pin_agent(agent_id)
    task = task_registry.create("title", "detail", parent=root_task_id)
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE agent_tasks SET reminder_count = 3, last_reminded_at = now(), "
            "escalated_at = now() WHERE id = %s",
            (task.id,),
        )
    db_conn.commit()
    task_registry.log(task.id, "note")
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT reminder_count, last_reminded_at, escalated_at FROM agent_tasks WHERE id = %s",
            (task.id,),
        )
        row = cur.fetchone()
    assert row == (0, None, None)


def test_log_bumps_updated_at(db_conn: psycopg.Connection, root_task_id: int) -> None:
    """A note append is real task activity: updated_at moves, so the task
    graph's last-activity window (Task #1969) keeps a task that only received
    log notes."""
    agent_id = _seed_agent(db_conn)
    pin_agent(agent_id)
    task = task_registry.create("log-activity", "detail", parent=root_task_id)
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE agent_tasks SET updated_at = now() - interval '30 days' WHERE id = %s",
            (task.id,),
        )
    db_conn.commit()
    task_registry.log(task.id, "note")
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT now() - updated_at < interval '1 hour' FROM agent_tasks WHERE id = %s",
            (task.id,),
        )
        row = cur.fetchone()
    assert row == (True,)


def test_log_missing_task_raises(db_conn: psycopg.Connection) -> None:
    agent_id = _seed_agent(db_conn)
    pin_agent(agent_id)
    with pytest.raises(ValueError, match="does not exist"):
        task_registry.log(999999, "note")


# ── create() — owner parameter ────────────────────────────────────────────


def _persisted_owner(db_conn, task_id: int) -> int | None:
    with db_conn.cursor() as cur:  # pyright: ignore[reportUnknownMemberType]
        cur.execute("SELECT owner FROM agent_tasks WHERE id = %s", (task_id,))  # pyright: ignore[reportUnknownMemberType]
        row = cur.fetchone()  # pyright: ignore[reportUnknownMemberType]
    assert row is not None
    return row[0]


def test_create_default_owner_is_creator(db_conn: psycopg.Connection, root_task_id: int) -> None:
    """When owner is not passed, the creating agent is the owner."""
    agent_id = _seed_agent(db_conn)
    pin_agent(agent_id)
    task = task_registry.create("title", "detail", parent=root_task_id)
    assert task.owner == agent_id
    assert _persisted_owner(db_conn, task.id) == agent_id


def test_create_explicit_owner(db_conn: psycopg.Connection, root_task_id: int) -> None:
    """When owner is explicitly set, that agent becomes the owner."""
    agent_id = _seed_agent(db_conn)
    other_id = _seed_agent(db_conn)
    pin_agent(agent_id)
    with record_notes(db_conn):
        task = task_registry.create("title", "detail", owner=other_id, parent=root_task_id)
    assert task.owner == other_id
    assert _persisted_owner(db_conn, task.id) == other_id


def test_create_with_owner_notifies_target(db_conn: psycopg.Connection, root_task_id: int) -> None:
    """When owner != creator, a notification message is sent to the owner."""
    agent_id = _seed_agent(db_conn)
    other_id = _seed_agent(db_conn)
    pin_agent(agent_id)
    with record_notes(db_conn) as mock_send:
        task = task_registry.create("title", "detail", owner=other_id, parent=root_task_id)
        mock_send.assert_called_once()
        call_args = mock_send.call_args
        assert call_args[0][0] == other_id
        msg = call_args[0][1]
        assert f"Task #{task.id}" in msg
        assert "title" in msg
        assert "detail" in msg
        assert "assigned to you" in msg
        # An assignment is a delegator direction: the new owner is
        # auto-resurrected so the task never strands on a dead agent.
        assert call_args.kwargs["task_id"] == task.id
        assert call_args.kwargs["resurrect"] is True


def test_create_with_owner_self_no_notification(
    db_conn: psycopg.Connection, root_task_id: int
) -> None:
    """When owner == creator, no notification is sent."""
    agent_id = _seed_agent(db_conn)
    pin_agent(agent_id)
    with record_notes(db_conn) as mock_send:
        task_registry.create("title", "detail", owner=agent_id, parent=root_task_id)
        mock_send.assert_not_called()


def test_create_without_owner_no_notification(
    db_conn: psycopg.Connection, root_task_id: int
) -> None:
    """When owner is not passed (default = creator), no notification is sent."""
    agent_id = _seed_agent(db_conn)
    pin_agent(agent_id)
    with record_notes(db_conn) as mock_send:
        task_registry.create("title", "detail", parent=root_task_id)
        mock_send.assert_not_called()


# ── update() — owner semantics ────────────────────────────────────────────


def test_update_owner_reassign_notifies(db_conn: psycopg.Connection, root_task_id: int) -> None:
    """When owner changes to a different agent, the new owner is notified.
    The old owner is skipped when they are the actor performing the update."""
    agent_id = _seed_agent(db_conn)
    other_id = _seed_agent(db_conn)
    pin_agent(agent_id)
    task = task_registry.create("title", "detail", parent=root_task_id)
    with record_notes(db_conn) as mock_send:
        task_registry.update(task.id, owner=other_id)
        # Only new owner notified; old owner == actor is skipped
        mock_send.assert_called_once()
        call_args = mock_send.call_args
        assert call_args[0][0] == other_id
        assert "now assigned" in call_args[0][1]
        assert call_args.kwargs["task_id"] == task.id
        assert call_args.kwargs["resurrect"] is True


def test_update_owner_none_is_noop(db_conn: psycopg.Connection, root_task_id: int) -> None:
    """owner=None means do not change, not release. Raises ValueError
    because nothing else is changing either."""
    agent_id = _seed_agent(db_conn)
    pin_agent(agent_id)
    task = task_registry.create("title", "detail", parent=root_task_id)
    with pytest.raises(ValueError, match="at least one"):
        task_registry.update(task.id, owner=None)
    # Owner should be unchanged
    assert _persisted_owner(db_conn, task.id) == agent_id


def test_update_owner_none_with_status_is_noop_for_owner(
    db_conn: psycopg.Connection, root_task_id: int
) -> None:
    """owner=None alongside a real change (status) only changes status, not owner."""
    agent_id = _seed_agent(db_conn)
    pin_agent(agent_id)
    task = task_registry.create("title", "detail", parent=root_task_id)
    task_registry.update(task.id, status="in_progress", owner=None)
    got = task_registry.get(task.id)
    assert got.status == "in_progress"
    assert got.owner == agent_id  # unchanged


def test_update_owner_self_no_notification(db_conn: psycopg.Connection, root_task_id: int) -> None:
    """Reassigning to yourself is a no-op notification-wise."""
    agent_id = _seed_agent(db_conn)
    pin_agent(agent_id)
    task = task_registry.create("title", "detail", parent=root_task_id)
    with record_notes(db_conn) as mock_send:
        task_registry.update(task.id, owner=agent_id)
        # send_system_note should not be called because old_owner == new_owner
        mock_send.assert_not_called()


def test_update_owner_new_terminated_still_notified(
    db_conn: psycopg.Connection, root_task_id: int
) -> None:
    """A terminated new owner IS notified — the system-note delivery is asked
    to auto-resurrect it (resurrect=True), so an assigned task never strands
    on a dead agent."""
    agent_id = _seed_agent(db_conn)
    dead_id = _seed_agent(db_conn, status="terminated")
    pin_agent(agent_id)
    task = task_registry.create("title", "detail", parent=root_task_id)
    with record_notes(db_conn) as mock_send:
        task_registry.update(task.id, owner=dead_id)
        mock_send.assert_called_once()
        assert mock_send.call_args[0][0] == dead_id
        assert "now assigned" in mock_send.call_args[0][1]
        assert mock_send.call_args.kwargs["resurrect"] is True


def test_update_owner_old_terminated_leg_skipped(db_conn: psycopg.Connection) -> None:
    """The previous-owner leg is the one deliberate terminated skip: reassigning
    a task away from a terminated owner tells only the new (live) owner."""
    actor_id = _seed_agent(db_conn)
    dead_old = _seed_agent(db_conn, status="terminated")
    new_owner = _seed_agent(db_conn)
    pin_agent(actor_id)
    # Seed a task already owned by the terminated agent (bypass create's
    # notify path) so the update's old_owner is the terminated one.
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_tasks (title, description, created_by, owner, remind_interval_seconds) "
            "VALUES ('t', 'd', %s, %s, 1800) RETURNING id",
            (str(actor_id), dead_old),
        )
        task_id = cur.fetchone()[0]  # type: ignore[index]
    db_conn.commit()
    with record_notes(db_conn) as mock_send:
        task_registry.update(task_id, owner=new_owner)  # pyright: ignore[reportUnknownArgumentType]
        # Only the new owner is told; the terminated old owner is skipped.
        mock_send.assert_called_once()
        assert mock_send.call_args[0][0] == new_owner


def test_failed_notification_policy_lookup_rolls_back_assignment(
    db_conn: psycopg.Connection,
) -> None:
    """A failed notification policy lookup cannot commit a task without its notes."""
    actor_id = _seed_agent(db_conn)
    old_owner = _seed_agent(db_conn)
    new_owner = _seed_agent(db_conn)
    pin_agent(actor_id)
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_tasks (title, description, created_by, owner, remind_interval_seconds) "
            "VALUES ('t', 'd', %s, %s, 1800) RETURNING id",
            (str(actor_id), old_owner),
        )
        task_id = cur.fetchone()[0]  # type: ignore[index]
    db_conn.commit()
    with (
        record_notes(db_conn) as send_note,
        # Fleet-plugin tests can replace the sys.modules entry in this xdist
        # worker. Patch the collection-time module that update() calls.
        patch.object(task_registry, "_is_terminated", side_effect=RuntimeError),
        pytest.raises(RuntimeError),
    ):
        task_registry.update(task_id, owner=new_owner)  # pyright: ignore[reportUnknownArgumentType]
    send_note.assert_not_called()
    with db_conn.cursor() as cur:
        cur.execute("SELECT owner FROM agent_tasks WHERE id=%s", (task_id,))
        assert cur.fetchone() == (old_owner,)


# ── update() — non-owner write notifies the owner ─────────────────────────


# ── priority ─────────────────────────────────────────────────────────────


# ── SSE task events ────────────────────────────────────────────────────────


# ── create_and_assign() ──────────────────────────────────────────────────


# ── create() — duplicate title rejection ──────────────────────────────────


# ── database-level constraints (migration agent-tasks-constraints) ─────────


def _fake_no_duplicate_precheck(real_execute):
    """Cursor.execute stand-in that lies to the app-level duplicate-title
    pre-check (reports no match) and delegates everything else. Lets a test
    drive the code past the friendly check into the database backstop."""

    def fake_execute(cur, query, *args, **kwargs):
        if query.startswith("SELECT id, status FROM agent_tasks WHERE title"):  # pyright: ignore[reportUnknownMemberType]
            real_execute(cur, "SELECT 1 WHERE FALSE")
            return None
        return real_execute(cur, query, *args, **kwargs)

    return fake_execute


# ── update() — title rename ────────────────────────────────────────────────


# ── update() — note parameter ────────────────────────────────────────────


# ── parent requirement & tree anchoring ──────────────────────────────────────


def _parent_of(db: psycopg.Connection, task_id: int) -> int | None:
    with db.cursor() as cur:
        cur.execute("SELECT parent_id FROM agent_tasks WHERE id = %s", (task_id,))
        row = cur.fetchone()
    assert row is not None
    return row[0]


def _persisted_parent(db: psycopg.Connection, task_id: int) -> int | None:
    with db.cursor() as cur:
        cur.execute("SELECT parent_id FROM agent_tasks WHERE id = %s", (task_id,))
        row = cur.fetchone()
    assert row is not None
    return row[0]
