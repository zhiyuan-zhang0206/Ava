"""Task registry cases: update non owner notifies owner."""

from __future__ import annotations

import inspect
from unittest.mock import patch

import psycopg
import pytest

from ava_builtins.plugins.ava_fleet import task_registry
from ava_builtins.plugins.ava_fleet.tests.task_registry.notes import record_notes
from ava_builtins.plugins.ava_fleet.tests.test_task_registry import (
    _fake_no_duplicate_precheck,
    _persisted_parent,
    _persisted_priority,
    _seed_agent,
)
from ava_builtins.plugins.ava_fleet.tests.test_task_registry import (
    root_task_id as root_task_id,
)
from base.events.live import announce
from tests.fixtures.pin_agent import pin_agent


def test_update_non_owner_notifies_owner(db_conn: psycopg.Connection, root_task_id: int) -> None:
    """A non-owner update (here: the motivating cancel case) notifies the owner
    with the task, the changed fields, and the author."""
    actor_id = _seed_agent(db_conn)
    owner_id = _seed_agent(db_conn)
    pin_agent(actor_id)
    with record_notes(db_conn):
        task = task_registry.create("title", "detail", owner=owner_id, parent=root_task_id)
    with record_notes(db_conn) as mock_send:
        task_registry.update(task.id, status="cancelled")
        mock_send.assert_called_once()
        recipient, msg = mock_send.call_args[0]
        assert recipient == owner_id
        assert f"Task #{task.id}" in msg
        assert '"title"' in msg
        assert "status → cancelled" in msg
        assert f"agent #{actor_id}" in msg
        # A plain update notice is a notification, not a delegator
        # direction: it must never resurrect a terminated owner.
        assert mock_send.call_args.kwargs["resurrect"] is False


def test_update_by_owner_no_notification(db_conn: psycopg.Connection, root_task_id: int) -> None:
    """The owner updating its own task is not notified about its own action."""
    agent_id = _seed_agent(db_conn)
    pin_agent(agent_id)
    task = task_registry.create("title", "detail", parent=root_task_id)
    with record_notes(db_conn) as mock_send:
        task_registry.update(task.id, status="done", results="shipped")
        mock_send.assert_not_called()


def test_update_non_owner_skips_terminated_owner(
    db_conn: psycopg.Connection, root_task_id: int
) -> None:
    """A terminated owner is NOT notified of a non-owner write — a notification
    is not worth resurrecting the agent (user ruling 2026-08-27: notification
    messages never auto-resurrect a terminated owner)."""
    actor_id = _seed_agent(db_conn)
    dead_owner = _seed_agent(db_conn, status="terminated")
    pin_agent(actor_id)
    with record_notes(db_conn):
        task = task_registry.create("title", "detail", owner=dead_owner, parent=root_task_id)
    with record_notes(db_conn) as mock_send:
        task_registry.update(task.id, status="done")
        mock_send.assert_not_called()


def test_update_parent_only_by_non_owner_no_notification(
    db_conn: psycopg.Connection, root_task_id: int
) -> None:
    """A parent-only reparent by a non-owner is structural tree maintenance, not
    a business change: no owner notification, even when the owner is live
    (regression for the 2026-08-27 batch-reparent wake storm)."""
    actor_id = _seed_agent(db_conn)
    owner_id = _seed_agent(db_conn)
    pin_agent(actor_id)
    with record_notes(db_conn):
        parent = task_registry.create("notify-parent", "d", parent=root_task_id)
        task = task_registry.create("notify-child", "d", owner=owner_id, parent=root_task_id)
    with record_notes(db_conn) as mock_send:
        task_registry.update(task.id, parent_id=parent.id)
        mock_send.assert_not_called()
    assert _persisted_parent(db_conn, task.id) == parent.id


def test_update_parent_only_terminated_owner_not_resurrected(
    db_conn: psycopg.Connection, root_task_id: int
) -> None:
    """The exact incident shape: a batch reparent moves a task owned by a
    terminated agent — the owner must stay asleep (no send_system_note, so no
    auto-resurrect)."""
    actor_id = _seed_agent(db_conn)
    dead_owner = _seed_agent(db_conn, status="terminated")
    pin_agent(actor_id)
    with record_notes(db_conn):
        parent = task_registry.create("storm-parent", "d", parent=root_task_id)
        task = task_registry.create("storm-child", "d", owner=dead_owner, parent=root_task_id)
    with record_notes(db_conn) as mock_send:
        task_registry.update(task.id, parent_id=parent.id)
        mock_send.assert_not_called()
    assert _persisted_parent(db_conn, task.id) == parent.id


def test_update_parent_plus_business_field_still_notifies(
    db_conn: psycopg.Connection, root_task_id: int
) -> None:
    """A reparent combined with a real business change is not parent-only: the
    owner is still notified, so a mixed update never slips through silently."""
    actor_id = _seed_agent(db_conn)
    owner_id = _seed_agent(db_conn)
    pin_agent(actor_id)
    with record_notes(db_conn):
        parent = task_registry.create("mixed-parent", "d", parent=root_task_id)
        task = task_registry.create("mixed-child", "d", owner=owner_id, parent=root_task_id)
    with record_notes(db_conn) as mock_send:
        task_registry.update(task.id, parent_id=parent.id, status="cancelled")
        mock_send.assert_called_once()
        recipient, msg = mock_send.call_args[0]
        assert recipient == owner_id
        assert "parent →" in msg
        assert "status → cancelled" in msg


def test_update_owner_change_appends_changes_to_new_owner(
    db_conn: psycopg.Connection, root_task_id: int
) -> None:
    """A reassignment that also edits the task carries the change summary in the
    new owner's message (one message), while the old owner gets the released
    notice; the generic update notice is not sent on top."""
    actor_id = _seed_agent(db_conn)
    old_owner = _seed_agent(db_conn)
    new_owner = _seed_agent(db_conn)
    pin_agent(actor_id)
    with record_notes(db_conn):
        task = task_registry.create("title", "detail", owner=old_owner, parent=root_task_id)
    with record_notes(db_conn) as mock_send:
        task_registry.update(task.id, status="cancelled", owner=new_owner)
        assert mock_send.call_count == 2
        msgs = {call.args[0]: call.args[1] for call in mock_send.call_args_list}
        flags = {call.args[0]: call.kwargs["resurrect"] for call in mock_send.call_args_list}
        assert "now assigned" in msgs[new_owner]
        assert "status → cancelled" in msgs[new_owner]
        assert "no longer assigned" in msgs[old_owner]
        # New owner (assignment): resurrect; old owner (released): never.
        assert flags == {new_owner: True, old_owner: False}


def test_log_by_non_owner_notifies_owner(db_conn: psycopg.Connection, root_task_id: int) -> None:
    """log() by a non-owner counts as a write: the owner hears about it too."""
    actor_id = _seed_agent(db_conn)
    owner_id = _seed_agent(db_conn)
    pin_agent(actor_id)
    with record_notes(db_conn):
        task = task_registry.create("title", "detail", owner=owner_id, parent=root_task_id)
    with record_notes(db_conn) as mock_send:
        task_registry.log(task.id, "progress update")
        mock_send.assert_called_once()
        assert mock_send.call_args[0][0] == owner_id
        assert "note appended" in mock_send.call_args[0][1]


def test_create_defaults_priority_p2(db_conn: psycopg.Connection, root_task_id: int) -> None:
    agent_id = _seed_agent(db_conn)
    pin_agent(agent_id)
    task = task_registry.create("title", "detail", parent=root_task_id)
    assert task.priority == "P2"
    assert _persisted_priority(db_conn, task.id) == "P2"


def test_create_honours_explicit_priority(db_conn: psycopg.Connection, root_task_id: int) -> None:
    agent_id = _seed_agent(db_conn)
    pin_agent(agent_id)
    task = task_registry.create("title", "detail", priority="P0", parent=root_task_id)
    assert task.priority == "P0"
    assert _persisted_priority(db_conn, task.id) == "P0"


def test_create_rejects_bad_priority(db_conn: psycopg.Connection, root_task_id: int) -> None:
    agent_id = _seed_agent(db_conn)
    pin_agent(agent_id)
    with pytest.raises(ValueError, match="priority must be one of"):
        task_registry.create("title", "detail", priority="P9", parent=root_task_id)


def test_update_changes_priority(db_conn: psycopg.Connection, root_task_id: int) -> None:
    agent_id = _seed_agent(db_conn)
    pin_agent(agent_id)
    task = task_registry.create("title", "detail", parent=root_task_id)
    task_registry.update(task.id, priority="P1")
    assert _persisted_priority(db_conn, task.id) == "P1"


def test_update_none_priority_is_noop(db_conn: psycopg.Connection, root_task_id: int) -> None:
    """priority=None means 'no change' — the task keeps its existing rung."""
    agent_id = _seed_agent(db_conn)
    pin_agent(agent_id)
    task = task_registry.create("title", "detail", priority="P0", parent=root_task_id)
    task_registry.update(task.id, status="in_progress", priority=None)
    assert _persisted_priority(db_conn, task.id) == "P0"


def test_update_rejects_bad_priority(db_conn: psycopg.Connection, root_task_id: int) -> None:
    agent_id = _seed_agent(db_conn)
    pin_agent(agent_id)
    task = task_registry.create("title", "detail", parent=root_task_id)
    with pytest.raises(ValueError, match="priority must be one of"):
        task_registry.update(task.id, priority="nope")


def test_create_publishes_task_created(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch, root_task_id: int
) -> None:
    agent_id = _seed_agent(db_conn)
    calls: list[tuple] = []
    monkeypatch.setattr(announce, "publish_task_created_sync", lambda *a: calls.append(a))  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
    pin_agent(agent_id)
    task = task_registry.create("title", "detail", parent=root_task_id)
    assert [call[1:] for call in calls] == [(agent_id, task.id)]


def test_update_publishes_task_updated(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch, root_task_id: int
) -> None:
    agent_id = _seed_agent(db_conn)
    pin_agent(agent_id)
    task = task_registry.create("title", "detail", parent=root_task_id)
    calls: list[tuple] = []
    monkeypatch.setattr(announce, "publish_task_updated_sync", lambda *a: calls.append(a))  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
    task_registry.update(task.id, status="in_progress")
    assert [call[1:] for call in calls] == [(agent_id, task.id)]


def test_create_and_assign_signature() -> None:
    """create_and_assign has the expected parameter names and defaults."""
    sig = inspect.signature(task_registry.create_and_assign)  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
    params = sig.parameters
    assert list(params.keys()) == [
        "title",
        "description",
        "operation_key",
        "preset",
        "label",
        "config_overlay",
        "machine",
        "parent",
        "remind_interval_seconds",
        "priority",
    ]
    assert params["operation_key"].default is inspect.Parameter.empty
    assert params["preset"].default is None
    assert params["label"].default is None
    assert params["config_overlay"].default is None
    assert params["machine"].default is None
    assert params["parent"].default is inspect.Parameter.empty  # required, no default
    assert params["remind_interval_seconds"].default is None
    assert params["priority"].default == "P2"


def test_create_duplicate_in_progress_title_raises(db_conn, root_task_id: int):
    """Creating a task with the same title as an in_progress task raises
    ValueError — a task is born in_progress (post-2026-08-29 the 'open'
    status no longer exists), so any live task blocks its title."""
    agent_id = _seed_agent(db_conn)  # pyright: ignore[reportUnknownArgumentType]
    pin_agent(agent_id)
    task_registry.create("unique title", "detail", parent=root_task_id)
    with pytest.raises(ValueError, match="already exists"):
        task_registry.create("unique title", "detail", parent=root_task_id)


def test_create_same_title_done_allowed(db_conn, root_task_id: int):
    """Creating a task with the same title as a done task is allowed."""
    agent_id = _seed_agent(db_conn)  # pyright: ignore[reportUnknownArgumentType]
    pin_agent(agent_id)
    task = task_registry.create("unique title 3", "detail", parent=root_task_id)
    task_registry.update(task.id, status="done")
    task2 = task_registry.create("unique title 3", "detail", parent=root_task_id)
    assert task2.id != task.id


def test_schema_created_by_rejects_non_agent_value(db_conn):
    """The created_by CHECK accepts agent ids and 'system'/'user'; anything
    else is rejected at the database, not only in app code."""
    with db_conn.cursor() as cur, pytest.raises(psycopg.errors.CheckViolation):  # pyright: ignore[reportUnknownMemberType]
        cur.execute(  # pyright: ignore[reportUnknownMemberType]
            "INSERT INTO agent_tasks (title, description, created_by) "
            "VALUES ('bad-creator', 'd', 'someone-else')"
        )
    db_conn.rollback()  # pyright: ignore[reportUnknownMemberType]


def test_schema_created_by_accepts_system_and_user(db_conn):
    """'system' (the seeded root) and 'user' (historical non-agent rows) stay
    legal under the CHECK."""
    with db_conn.cursor() as cur:  # pyright: ignore[reportUnknownMemberType]
        cur.execute(  # pyright: ignore[reportUnknownMemberType]
            "INSERT INTO agent_tasks (title, description, status, created_by) "
            "VALUES ('sys-task', 'd', 'done', 'system') RETURNING id"
        )
        cur.execute(  # pyright: ignore[reportUnknownMemberType]
            "INSERT INTO agent_tasks (title, description, status, created_by) "
            "VALUES ('user-task', 'd', 'done', 'user') RETURNING id"
        )
    db_conn.commit()  # pyright: ignore[reportUnknownMemberType]


def test_schema_unique_in_progress_title_backstop(db_conn):
    """The partial unique index rejects a second in_progress task with a
    duplicate title, but allows the title once the earlier task leaves
    in_progress — the database mirror of the app-level rule."""
    with db_conn.cursor() as cur:  # pyright: ignore[reportUnknownMemberType]
        cur.execute(  # pyright: ignore[reportUnknownMemberType]
            "INSERT INTO agent_tasks (title, description, created_by) "
            "VALUES ('dup', 'd', 'system') RETURNING id"
        )
        first = cur.fetchone()[0]  # type: ignore[index]
        with pytest.raises(psycopg.errors.UniqueViolation):
            cur.execute(  # pyright: ignore[reportUnknownMemberType]
                "INSERT INTO agent_tasks (title, description, created_by) "
                "VALUES ('dup', 'd', 'system')"
            )
    db_conn.rollback()  # pyright: ignore[reportUnknownMemberType]
    with db_conn.cursor() as cur:  # pyright: ignore[reportUnknownMemberType]
        cur.execute("UPDATE agent_tasks SET status = 'done' WHERE id = %s", (first,))  # pyright: ignore[reportUnknownMemberType]
        cur.execute(  # pyright: ignore[reportUnknownMemberType]
            "INSERT INTO agent_tasks (title, description, created_by) "
            "VALUES ('dup', 'd', 'system') RETURNING id"
        )
    db_conn.commit()  # pyright: ignore[reportUnknownMemberType]


def test_create_unique_violation_race_becomes_value_error(db_conn, root_task_id: int):
    """When two creates race past the app-level pre-check, the partial unique
    index rejects the second INSERT with UniqueViolation; create() translates
    that into the same ValueError agents see on the normal path."""
    agent_id = _seed_agent(db_conn)  # pyright: ignore[reportUnknownArgumentType]
    pin_agent(agent_id)
    task_registry.create("raced-title", "d", parent=root_task_id)
    real_execute = psycopg.Cursor.execute
    with (
        patch.object(psycopg.Cursor, "execute", _fake_no_duplicate_precheck(real_execute)),  # pyright: ignore[reportUnknownArgumentType]
        pytest.raises(ValueError, match="already exists"),
    ):
        task_registry.create("raced-title", "d", parent=root_task_id)


def test_update_rename_race_becomes_value_error(db_conn, root_task_id: int):
    """Same backstop on the rename path: a concurrent rename that lands
    between update()'s pre-check and its UPDATE surfaces as ValueError, not a
    raw psycopg error."""
    agent_id = _seed_agent(db_conn)  # pyright: ignore[reportUnknownArgumentType]
    pin_agent(agent_id)
    task_registry.create("taken-title", "d", parent=root_task_id)
    task = task_registry.create("free-title", "d", parent=root_task_id)
    real_execute = psycopg.Cursor.execute
    with (
        patch.object(psycopg.Cursor, "execute", _fake_no_duplicate_precheck(real_execute)),  # pyright: ignore[reportUnknownArgumentType]
        pytest.raises(ValueError, match="already exists"),
    ):
        task_registry.update(task.id, title="taken-title")
    # The failed update left the row untouched.
    assert task_registry.get(task.id).title == "free-title"


def test_update_title_renames(db_conn, root_task_id: int):
    agent_id = _seed_agent(db_conn)  # pyright: ignore[reportUnknownArgumentType]
    pin_agent(agent_id)
    task = task_registry.create("old title", "detail", parent=root_task_id)
    task_registry.update(task.id, title="new title")
    assert task_registry.get(task.id).title == "new title"


def test_update_title_duplicate_in_progress_raises(db_conn, root_task_id: int):
    """Renaming to another in_progress task's title raises ValueError —
    the same invariant create() enforces."""
    agent_id = _seed_agent(db_conn)  # pyright: ignore[reportUnknownArgumentType]
    pin_agent(agent_id)
    task_registry.create("taken title", "detail", parent=root_task_id)
    task = task_registry.create("free title", "detail", parent=root_task_id)
    with pytest.raises(ValueError, match="already exists"):
        task_registry.update(task.id, title="taken title")
    assert task_registry.get(task.id).title == "free title"


def test_update_title_reassign_notifies_with_new_title(
    db_conn: psycopg.Connection, root_task_id: int
) -> None:
    """A rename and a reassignment in one update() notify with the new title."""
    agent_id = _seed_agent(db_conn)
    other_id = _seed_agent(db_conn)
    pin_agent(agent_id)
    task = task_registry.create("old title", "detail", parent=root_task_id)
    with record_notes(db_conn) as mock_send:
        task_registry.update(task.id, title="new title", owner=other_id)
    assert any("new title" in call.args[1] for call in mock_send.call_args_list)


def test_create_same_title_cancelled_allowed(db_conn, root_task_id: int):
    """Creating a task with the same title as a cancelled task is allowed."""
    agent_id = _seed_agent(db_conn)  # pyright: ignore[reportUnknownArgumentType]
    pin_agent(agent_id)
    task = task_registry.create("unique title 4", "detail", parent=root_task_id)
    task_registry.update(task.id, status="cancelled")
    task2 = task_registry.create("unique title 4", "detail", parent=root_task_id)
    assert task2.id != task.id
