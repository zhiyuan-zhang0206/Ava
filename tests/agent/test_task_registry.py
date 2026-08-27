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
from pathlib import Path
from unittest.mock import patch

import psycopg
import pytest

import ava
import ava._boot
from ava_builtins.plugins.ava_fleet import task_registry


def _seed_agent(db: psycopg.Connection, *, status: str = "running") -> int:
    with db.cursor() as cur:
        cur.execute("INSERT INTO agents DEFAULT VALUES RETURNING id")
        aid = cur.fetchone()[0]  # type: ignore[index]
        cur.execute(
            "INSERT INTO agents_meta (id, spawner, status) VALUES (%s, 'test', %s)",
            (aid, status),  # pyright: ignore[reportUnknownArgumentType]
        )
    db.commit()
    return aid


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
        "brief",
    ]
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
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        task = task_registry.create("title", "detail", priority=priority, parent=root_task_id)
        assert task.remind_interval_seconds == expected
        assert _persisted_remind_interval_seconds(db_conn, task.id) == expected
        assert _persisted_priority(db_conn, task.id) == priority
    finally:
        ava._boot._agent_id = original


def test_create_default_priority_is_p2_with_2h_interval(
    db_conn: psycopg.Connection, root_task_id: int
) -> None:
    agent_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        task = task_registry.create("title", "detail", parent=root_task_id)
        assert task.remind_interval_seconds == 7200
        assert _persisted_remind_interval_seconds(db_conn, task.id) == 7200
    finally:
        ava._boot._agent_id = original


def test_create_explicit_interval_beats_priority_default(
    db_conn: psycopg.Connection, root_task_id: int
) -> None:
    """An explicit interval wins over the priority's default (user override)."""
    agent_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        task = task_registry.create(
            "title", "detail", priority="P3", remind_interval_seconds=1800, parent=root_task_id
        )
        assert task.remind_interval_seconds == 1800
        assert _persisted_remind_interval_seconds(db_conn, task.id) == 1800
    finally:
        ava._boot._agent_id = original


def test_create_honours_explicit_value(db_conn: psycopg.Connection, root_task_id: int) -> None:
    agent_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        task = task_registry.create(
            "title", "detail", remind_interval_seconds=3600, parent=root_task_id
        )
        assert task.remind_interval_seconds == 3600
        assert _persisted_remind_interval_seconds(db_conn, task.id) == 3600
    finally:
        ava._boot._agent_id = original


def test_create_none_falls_back_to_default(db_conn: psycopg.Connection, root_task_id: int) -> None:
    """Reminders cannot be disabled: create(remind_interval_seconds=None) uses the
    priority default rather than writing NULL."""
    agent_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        task = task_registry.create(
            "title", "detail", remind_interval_seconds=None, parent=root_task_id
        )
        assert task.remind_interval_seconds == 7200  # P2 default -> 2h
        assert _persisted_remind_interval_seconds(db_conn, task.id) == 7200
    finally:
        ava._boot._agent_id = original


def test_create_rejects_non_positive_interval(
    db_conn: psycopg.Connection, root_task_id: int
) -> None:
    agent_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        with pytest.raises(ValueError, match="positive number of seconds"):
            task_registry.create("title", "detail", remind_interval_seconds=0, parent=root_task_id)
    finally:
        ava._boot._agent_id = original


def test_create_rejects_interval_over_24h(db_conn: psycopg.Connection, root_task_id: int) -> None:
    agent_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        with pytest.raises(ValueError, match="cannot be disabled"):
            task_registry.create(
                "title", "detail", remind_interval_seconds=86401, parent=root_task_id
            )
    finally:
        ava._boot._agent_id = original


def test_create_honours_24h_boundary(db_conn: psycopg.Connection, root_task_id: int) -> None:
    """Exactly 24h (86400s) is the largest accepted interval."""
    agent_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        task = task_registry.create(
            "title", "detail", remind_interval_seconds=86400, parent=root_task_id
        )
        assert task.remind_interval_seconds == 86400
    finally:
        ava._boot._agent_id = original


def test_update_changes_remind_interval_seconds(
    db_conn: psycopg.Connection, root_task_id: int
) -> None:
    agent_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        task = task_registry.create("title", "detail", parent=root_task_id)
        assert task.remind_interval_seconds == 7200  # P2 default -> 2h
        task_registry.update(task.id, remind_interval_seconds=1800)
        assert _persisted_remind_interval_seconds(db_conn, task.id) == 1800
    finally:
        ava._boot._agent_id = original


def test_update_none_remind_interval_is_noop(
    db_conn: psycopg.Connection, root_task_id: int
) -> None:
    """remind_interval_seconds=None means "no change" (reminders cannot be disabled),
    not "write NULL"; alongside a real change it leaves the interval intact."""
    agent_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        task = task_registry.create("title", "detail", parent=root_task_id)
        task_registry.update(task.id, status="in_progress", remind_interval_seconds=None)
        assert _persisted_remind_interval_seconds(db_conn, task.id) == 7200  # P2 default
    finally:
        ava._boot._agent_id = original


def test_update_rejects_interval_over_24h(db_conn: psycopg.Connection, root_task_id: int) -> None:
    agent_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        task = task_registry.create("title", "detail", parent=root_task_id)
        with pytest.raises(ValueError, match="cannot be disabled"):
            task_registry.update(task.id, remind_interval_seconds=86401)
    finally:
        ava._boot._agent_id = original


def test_update_resets_reminder_count(db_conn: psycopg.Connection, root_task_id: int) -> None:
    agent_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        task = task_registry.create("title", "detail", parent=root_task_id)
        # Simulate some prior reminders
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE agent_tasks SET reminder_count = 3, last_reminded_at = now() WHERE id = %s",
                (task.id,),
            )
        db_conn.commit()
        # An update resets the counters
        task_registry.update(task.id, results="progress")
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT reminder_count, last_reminded_at FROM agent_tasks WHERE id = %s",
                (task.id,),
            )
            row = cur.fetchone()
            assert row is not None
            reminder_count, last_reminded_at = row
        assert reminder_count == 0
        assert last_reminded_at is None
    finally:
        ava._boot._agent_id = original


def test_update_description(db_conn: psycopg.Connection, root_task_id: int) -> None:
    agent_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        task = task_registry.create("title", "detail", parent=root_task_id)
        task_registry.update(task.id, description="revised detail")
        assert task_registry.get(task.id).description == "revised detail"
    finally:
        ava._boot._agent_id = original


def test_update_nothing_raises(db_conn: psycopg.Connection, root_task_id: int) -> None:
    agent_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        task = task_registry.create("title", "detail", parent=root_task_id)
        with pytest.raises(ValueError, match="at least one"):
            task_registry.update(task.id)
    finally:
        ava._boot._agent_id = original


@pytest.mark.parametrize("closing_status", ["done", "cancelled"])
def test_update_rejects_closing_parent_with_open_child(
    db_conn: psycopg.Connection, closing_status: str, root_task_id: int
) -> None:
    agent_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        parent = task_registry.create(f"parent-{closing_status}", "detail", parent=root_task_id)
        child = task_registry.create(f"open-child-{closing_status}", "detail", parent=parent.id)
        message = (
            f"task {parent.id} has 1 open/in_progress child tasks (e.g. #{child.id}) — "
            "close or cancel them first"
        )

        with pytest.raises(ValueError, match=re.escape(message)):
            task_registry.update(parent.id, status=closing_status)

        assert task_registry.get(parent.id).status == "open"
    finally:
        ava._boot._agent_id = original


def test_update_rejects_closing_parent_with_in_progress_child(
    db_conn: psycopg.Connection, root_task_id: int
) -> None:
    agent_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        parent = task_registry.create("parent-with-active-child", "detail", parent=root_task_id)
        child = task_registry.create("active-child", "detail", parent=parent.id)
        task_registry.update(child.id, status="in_progress")

        with pytest.raises(ValueError, match=rf"task {parent.id} has 1 .*#{child.id}"):
            task_registry.update(parent.id, status="done")

        assert task_registry.get(parent.id).status == "open"
    finally:
        ava._boot._agent_id = original


def test_update_closes_parent_when_all_children_are_closed(
    db_conn: psycopg.Connection, root_task_id: int
) -> None:
    agent_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        parent = task_registry.create("parent-with-closed-children", "detail", parent=root_task_id)
        done_child = task_registry.create("done-child", "detail", parent=parent.id)
        cancelled_child = task_registry.create("cancelled-child", "detail", parent=parent.id)
        task_registry.update(done_child.id, status="done")
        task_registry.update(cancelled_child.id, status="cancelled")

        task_registry.update(parent.id, status="done")

        assert task_registry.get(parent.id).status == "done"
    finally:
        ava._boot._agent_id = original


@pytest.mark.parametrize("closing_status", ["done", "cancelled"])
def test_update_closes_parent_without_children(
    db_conn: psycopg.Connection, closing_status: str, root_task_id: int
) -> None:
    agent_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        task = task_registry.create(
            f"parent-without-children-{closing_status}", "detail", parent=root_task_id
        )

        task_registry.update(task.id, status=closing_status)

        assert task_registry.get(task.id).status == closing_status
    finally:
        ava._boot._agent_id = original


def test_update_starts_parent_with_open_child(
    db_conn: psycopg.Connection, root_task_id: int
) -> None:
    agent_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        parent = task_registry.create("parent-to-start", "detail", parent=root_task_id)
        task_registry.create("open-child-of-started-parent", "detail", parent=parent.id)

        task_registry.update(parent.id, status="in_progress")

        assert task_registry.get(parent.id).status == "in_progress"
    finally:
        ava._boot._agent_id = original


def test_update_title_and_note_on_parent_with_open_child(
    db_conn: psycopg.Connection, root_task_id: int
) -> None:
    agent_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        parent = task_registry.create("parent-to-edit", "detail", parent=root_task_id)
        task_registry.create("open-child-of-edited-parent", "detail", parent=parent.id)

        task_registry.update(parent.id, title="edited-parent", note="still active")

        updated = task_registry.get(parent.id)
        assert updated.status == "open"
        assert updated.title == "edited-parent"
        assert updated.results is not None
        assert updated.results.splitlines()[-1].endswith("still active")
    finally:
        ava._boot._agent_id = original


def test_log_appends_timestamped_lines(db_conn: psycopg.Connection, root_task_id: int) -> None:
    agent_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
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
    finally:
        ava._boot._agent_id = original


def test_log_stamps_in_the_cluster_timezone(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch, root_task_id: int
) -> None:
    """A task note is stamped in `settings.general.timezone`, not the writing
    machine's local timezone. A fleet spans machines and one task's notes are
    appended by several of them, so host-local stamps put unmarked, mutually
    inconsistent wall clocks in one column of text."""
    from shared.config import settings

    agent_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
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
    finally:
        ava._boot._agent_id = original


def test_log_preserves_replaced_results(db_conn: psycopg.Connection, root_task_id: int) -> None:
    agent_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        task = task_registry.create("title", "detail", parent=root_task_id)
        task_registry.update(task.id, results="prior log without newline")
        task_registry.log(task.id, "appended")
        results = task_registry.get(task.id).results
        assert results is not None
        lines = results.splitlines()
        assert lines[0] == "prior log without newline"
        assert lines[1].endswith("appended")
    finally:
        ava._boot._agent_id = original


def test_log_resets_reminder_count(db_conn: psycopg.Connection, root_task_id: int) -> None:
    agent_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        task = task_registry.create("title", "detail", parent=root_task_id)
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE agent_tasks SET reminder_count = 3, last_reminded_at = now() WHERE id = %s",
                (task.id,),
            )
        db_conn.commit()
        task_registry.log(task.id, "note")
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT reminder_count, last_reminded_at FROM agent_tasks WHERE id = %s",
                (task.id,),
            )
            row = cur.fetchone()
        assert row == (0, None)
    finally:
        ava._boot._agent_id = original


def test_log_missing_task_raises(db_conn: psycopg.Connection) -> None:
    agent_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        with pytest.raises(ValueError, match="does not exist"):
            task_registry.log(999999, "note")
    finally:
        ava._boot._agent_id = original


def test_deprecated_aliases_still_work(db_conn: psycopg.Connection, root_task_id: int) -> None:
    agent_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        task = task_registry.create("title", brief="via alias", parent=root_task_id)
        assert task.description == "via alias"
        assert task.brief == "via alias"
        task_registry.update(task.id, content="log via alias")
        got = task_registry.get(task.id)
        assert got.results == "log via alias"
        assert got.content == "log via alias"
    finally:
        ava._boot._agent_id = original


def test_alias_and_new_name_together_raise(db_conn: psycopg.Connection, root_task_id: int) -> None:
    agent_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        with pytest.raises(TypeError, match="deprecated alias"):
            task_registry.create("title", "detail", brief="also detail", parent=root_task_id)
        task = task_registry.create("title", "detail", parent=root_task_id)
        with pytest.raises(TypeError, match="deprecated alias"):
            task_registry.update(task.id, results="a", content="b")
    finally:
        ava._boot._agent_id = original


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
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        task = task_registry.create("title", "detail", parent=root_task_id)
        assert task.owner == agent_id
        assert _persisted_owner(db_conn, task.id) == agent_id
    finally:
        ava._boot._agent_id = original


def test_create_explicit_owner(db_conn: psycopg.Connection, root_task_id: int) -> None:
    """When owner is explicitly set, that agent becomes the owner."""
    agent_id = _seed_agent(db_conn)
    other_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        with patch("ava.agents.send_message"):
            task = task_registry.create("title", "detail", owner=other_id, parent=root_task_id)
        assert task.owner == other_id
        assert _persisted_owner(db_conn, task.id) == other_id
    finally:
        ava._boot._agent_id = original


def test_create_with_owner_notifies_target(db_conn: psycopg.Connection, root_task_id: int) -> None:
    """When owner != creator, a notification message is sent to the owner."""
    agent_id = _seed_agent(db_conn)
    other_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        with patch("ava.agents.send_message") as mock_send:
            task = task_registry.create("title", "detail", owner=other_id, parent=root_task_id)
            mock_send.assert_called_once()
            call_args = mock_send.call_args
            assert call_args[0][0] == other_id
            msg = call_args[0][1]
            assert f"Task #{task.id}" in msg
            assert "title" in msg
            assert "detail" in msg
            assert "assigned to you" in msg
    finally:
        ava._boot._agent_id = original


def test_create_with_owner_self_no_notification(
    db_conn: psycopg.Connection, root_task_id: int
) -> None:
    """When owner == creator, no notification is sent."""
    agent_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        with patch("ava.agents.send_message") as mock_send:
            task_registry.create("title", "detail", owner=agent_id, parent=root_task_id)
            mock_send.assert_not_called()
    finally:
        ava._boot._agent_id = original


def test_create_without_owner_no_notification(
    db_conn: psycopg.Connection, root_task_id: int
) -> None:
    """When owner is not passed (default = creator), no notification is sent."""
    agent_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        with patch("ava.agents.send_message") as mock_send:
            task_registry.create("title", "detail", parent=root_task_id)
            mock_send.assert_not_called()
    finally:
        ava._boot._agent_id = original


# ── update() — owner semantics ────────────────────────────────────────────


def test_update_owner_reassign_notifies(db_conn: psycopg.Connection, root_task_id: int) -> None:
    """When owner changes to a different agent, the new owner is notified.
    The old owner is skipped when they are the actor performing the update."""
    agent_id = _seed_agent(db_conn)
    other_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        task = task_registry.create("title", "detail", parent=root_task_id)
        with patch("ava.agents.send_message") as mock_send:
            task_registry.update(task.id, owner=other_id)
            # Only new owner notified; old owner == actor is skipped
            mock_send.assert_called_once()
            call_args = mock_send.call_args
            assert call_args[0][0] == other_id
            assert "now assigned" in call_args[0][1]
    finally:
        ava._boot._agent_id = original


def test_update_owner_none_is_noop(db_conn: psycopg.Connection, root_task_id: int) -> None:
    """owner=None means do not change, not release. Raises ValueError
    because nothing else is changing either."""
    agent_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        task = task_registry.create("title", "detail", parent=root_task_id)
        with pytest.raises(ValueError, match="at least one"):
            task_registry.update(task.id, owner=None)
        # Owner should be unchanged
        assert _persisted_owner(db_conn, task.id) == agent_id
    finally:
        ava._boot._agent_id = original


def test_update_owner_none_with_status_is_noop_for_owner(
    db_conn: psycopg.Connection, root_task_id: int
) -> None:
    """owner=None alongside a real change (status) only changes status, not owner."""
    agent_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        task = task_registry.create("title", "detail", parent=root_task_id)
        task_registry.update(task.id, status="in_progress", owner=None)
        got = task_registry.get(task.id)
        assert got.status == "in_progress"
        assert got.owner == agent_id  # unchanged
    finally:
        ava._boot._agent_id = original


def test_update_owner_self_no_notification(db_conn: psycopg.Connection, root_task_id: int) -> None:
    """Reassigning to yourself is a no-op notification-wise."""
    agent_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        task = task_registry.create("title", "detail", parent=root_task_id)
        with patch("ava.agents.send_message") as mock_send:
            task_registry.update(task.id, owner=agent_id)
            # send_message should not be called because old_owner == new_owner
            mock_send.assert_not_called()
    finally:
        ava._boot._agent_id = original


def test_update_owner_new_terminated_still_notified(
    db_conn: psycopg.Connection, root_task_id: int
) -> None:
    """A terminated new owner IS notified — send_message auto-resurrects it, so
    an assigned task never strands on a dead agent."""
    agent_id = _seed_agent(db_conn)
    dead_id = _seed_agent(db_conn, status="terminated")
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        task = task_registry.create("title", "detail", parent=root_task_id)
        with patch("ava.agents.send_message") as mock_send:
            task_registry.update(task.id, owner=dead_id)
            mock_send.assert_called_once()
            assert mock_send.call_args[0][0] == dead_id
            assert "now assigned" in mock_send.call_args[0][1]
    finally:
        ava._boot._agent_id = original


def test_update_owner_old_terminated_leg_skipped(db_conn: psycopg.Connection) -> None:
    """The previous-owner leg is the one deliberate terminated skip: reassigning
    a task away from a terminated owner tells only the new (live) owner."""
    actor_id = _seed_agent(db_conn)
    dead_old = _seed_agent(db_conn, status="terminated")
    new_owner = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = actor_id
    try:
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
        with patch("ava.agents.send_message") as mock_send:
            task_registry.update(task_id, owner=new_owner)  # pyright: ignore[reportUnknownArgumentType]
            # Only the new owner is told; the terminated old owner is skipped.
            mock_send.assert_called_once()
            assert mock_send.call_args[0][0] == new_owner
    finally:
        ava._boot._agent_id = original


# ── update() — non-owner write notifies the owner ─────────────────────────


def test_update_non_owner_notifies_owner(db_conn: psycopg.Connection, root_task_id: int) -> None:
    """A non-owner update (here: the motivating cancel case) notifies the owner
    with the task, the changed fields, and the author."""
    actor_id = _seed_agent(db_conn)
    owner_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = actor_id
    try:
        with patch("ava.agents.send_message"):
            task = task_registry.create("title", "detail", owner=owner_id, parent=root_task_id)
        with patch("ava.agents.send_message") as mock_send:
            task_registry.update(task.id, status="cancelled")
            mock_send.assert_called_once()
            recipient, msg = mock_send.call_args[0]
            assert recipient == owner_id
            assert f"Task #{task.id}" in msg
            assert '"title"' in msg
            assert "status → cancelled" in msg
            assert f"agent #{actor_id}" in msg
    finally:
        ava._boot._agent_id = original


def test_update_by_owner_no_notification(db_conn: psycopg.Connection, root_task_id: int) -> None:
    """The owner updating its own task is not notified about its own action."""
    agent_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        task = task_registry.create("title", "detail", parent=root_task_id)
        with patch("ava.agents.send_message") as mock_send:
            task_registry.update(task.id, status="done", results="shipped")
            mock_send.assert_not_called()
    finally:
        ava._boot._agent_id = original


def test_update_non_owner_skips_terminated_owner(
    db_conn: psycopg.Connection, root_task_id: int
) -> None:
    """A terminated owner is NOT notified of a non-owner write — a notification
    is not worth resurrecting the agent (user ruling 2026-08-27: notification
    messages never auto-resurrect a terminated owner)."""
    actor_id = _seed_agent(db_conn)
    dead_owner = _seed_agent(db_conn, status="terminated")
    original = ava._boot._agent_id
    ava._boot._agent_id = actor_id
    try:
        with patch("ava.agents.send_message"):
            task = task_registry.create("title", "detail", owner=dead_owner, parent=root_task_id)
        with patch("ava.agents.send_message") as mock_send:
            task_registry.update(task.id, status="done")
            mock_send.assert_not_called()
    finally:
        ava._boot._agent_id = original


def test_update_parent_only_by_non_owner_no_notification(
    db_conn: psycopg.Connection, root_task_id: int
) -> None:
    """A parent-only reparent by a non-owner is structural tree maintenance, not
    a business change: no owner notification, even when the owner is live
    (regression for the 2026-08-27 batch-reparent wake storm)."""
    actor_id = _seed_agent(db_conn)
    owner_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = actor_id
    try:
        with patch("ava.agents.send_message"):
            parent = task_registry.create("notify-parent", "d", parent=root_task_id)
            task = task_registry.create("notify-child", "d", owner=owner_id, parent=root_task_id)
        with patch("ava.agents.send_message") as mock_send:
            task_registry.update(task.id, parent_id=parent.id)
            mock_send.assert_not_called()
        assert _persisted_parent(db_conn, task.id) == parent.id
    finally:
        ava._boot._agent_id = original


def test_update_parent_only_terminated_owner_not_resurrected(
    db_conn: psycopg.Connection, root_task_id: int
) -> None:
    """The exact incident shape: a batch reparent moves a task owned by a
    terminated agent — the owner must stay asleep (no send_message, so no
    auto-resurrect)."""
    actor_id = _seed_agent(db_conn)
    dead_owner = _seed_agent(db_conn, status="terminated")
    original = ava._boot._agent_id
    ava._boot._agent_id = actor_id
    try:
        with patch("ava.agents.send_message"):
            parent = task_registry.create("storm-parent", "d", parent=root_task_id)
            task = task_registry.create("storm-child", "d", owner=dead_owner, parent=root_task_id)
        with patch("ava.agents.send_message") as mock_send:
            task_registry.update(task.id, parent_id=parent.id)
            mock_send.assert_not_called()
        assert _persisted_parent(db_conn, task.id) == parent.id
    finally:
        ava._boot._agent_id = original


def test_update_parent_plus_business_field_still_notifies(
    db_conn: psycopg.Connection, root_task_id: int
) -> None:
    """A reparent combined with a real business change is not parent-only: the
    owner is still notified, so a mixed update never slips through silently."""
    actor_id = _seed_agent(db_conn)
    owner_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = actor_id
    try:
        with patch("ava.agents.send_message"):
            parent = task_registry.create("mixed-parent", "d", parent=root_task_id)
            task = task_registry.create("mixed-child", "d", owner=owner_id, parent=root_task_id)
        with patch("ava.agents.send_message") as mock_send:
            task_registry.update(task.id, parent_id=parent.id, status="cancelled")
            mock_send.assert_called_once()
            recipient, msg = mock_send.call_args[0]
            assert recipient == owner_id
            assert "parent →" in msg
            assert "status → cancelled" in msg
    finally:
        ava._boot._agent_id = original


def test_update_owner_change_appends_changes_to_new_owner(
    db_conn: psycopg.Connection, root_task_id: int
) -> None:
    """A reassignment that also edits the task carries the change summary in the
    new owner's message (one message), while the old owner gets the released
    notice; the generic update notice is not sent on top."""
    actor_id = _seed_agent(db_conn)
    old_owner = _seed_agent(db_conn)
    new_owner = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = actor_id
    try:
        with patch("ava.agents.send_message"):
            task = task_registry.create("title", "detail", owner=old_owner, parent=root_task_id)
        with patch("ava.agents.send_message") as mock_send:
            task_registry.update(task.id, status="cancelled", owner=new_owner)
            assert mock_send.call_count == 2
            msgs = {call.args[0]: call.args[1] for call in mock_send.call_args_list}
            assert "now assigned" in msgs[new_owner]
            assert "status → cancelled" in msgs[new_owner]
            assert "no longer assigned" in msgs[old_owner]
    finally:
        ava._boot._agent_id = original


def test_log_by_non_owner_notifies_owner(db_conn: psycopg.Connection, root_task_id: int) -> None:
    """log() by a non-owner counts as a write: the owner hears about it too."""
    actor_id = _seed_agent(db_conn)
    owner_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = actor_id
    try:
        with patch("ava.agents.send_message"):
            task = task_registry.create("title", "detail", owner=owner_id, parent=root_task_id)
        with patch("ava.agents.send_message") as mock_send:
            task_registry.log(task.id, "progress update")
            mock_send.assert_called_once()
            assert mock_send.call_args[0][0] == owner_id
            assert "note appended" in mock_send.call_args[0][1]
    finally:
        ava._boot._agent_id = original


# ── priority ─────────────────────────────────────────────────────────────


def test_create_defaults_priority_p2(db_conn: psycopg.Connection, root_task_id: int) -> None:
    agent_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        task = task_registry.create("title", "detail", parent=root_task_id)
        assert task.priority == "P2"
        assert _persisted_priority(db_conn, task.id) == "P2"
    finally:
        ava._boot._agent_id = original


def test_create_honours_explicit_priority(db_conn: psycopg.Connection, root_task_id: int) -> None:
    agent_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        task = task_registry.create("title", "detail", priority="P0", parent=root_task_id)
        assert task.priority == "P0"
        assert _persisted_priority(db_conn, task.id) == "P0"
    finally:
        ava._boot._agent_id = original


def test_create_rejects_bad_priority(db_conn: psycopg.Connection, root_task_id: int) -> None:
    agent_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        with pytest.raises(ValueError, match="priority must be one of"):
            task_registry.create("title", "detail", priority="P9", parent=root_task_id)
    finally:
        ava._boot._agent_id = original


def test_update_changes_priority(db_conn: psycopg.Connection, root_task_id: int) -> None:
    agent_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        task = task_registry.create("title", "detail", parent=root_task_id)
        task_registry.update(task.id, priority="P1")
        assert _persisted_priority(db_conn, task.id) == "P1"
    finally:
        ava._boot._agent_id = original


def test_update_none_priority_is_noop(db_conn: psycopg.Connection, root_task_id: int) -> None:
    """priority=None means 'no change' — the task keeps its existing rung."""
    agent_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        task = task_registry.create("title", "detail", priority="P0", parent=root_task_id)
        task_registry.update(task.id, status="in_progress", priority=None)
        assert _persisted_priority(db_conn, task.id) == "P0"
    finally:
        ava._boot._agent_id = original


def test_update_rejects_bad_priority(db_conn: psycopg.Connection, root_task_id: int) -> None:
    agent_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        task = task_registry.create("title", "detail", parent=root_task_id)
        with pytest.raises(ValueError, match="priority must be one of"):
            task_registry.update(task.id, priority="nope")
    finally:
        ava._boot._agent_id = original


# ── SSE task events ────────────────────────────────────────────────────────


def test_create_publishes_task_created(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch, root_task_id: int
) -> None:
    agent_id = _seed_agent(db_conn)
    calls: list[tuple] = []
    monkeypatch.setattr(task_registry, "publish_task_created_sync", lambda *a: calls.append(a))  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        task = task_registry.create("title", "detail", parent=root_task_id)
    finally:
        ava._boot._agent_id = original
    assert calls == [(agent_id, task.id)]


def test_update_publishes_task_updated(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch, root_task_id: int
) -> None:
    agent_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        task = task_registry.create("title", "detail", parent=root_task_id)
        calls: list[tuple] = []
        monkeypatch.setattr(task_registry, "publish_task_updated_sync", lambda *a: calls.append(a))  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
        task_registry.update(task.id, status="in_progress")
    finally:
        ava._boot._agent_id = original
    assert calls == [(agent_id, task.id)]


# ── create_and_assign() ──────────────────────────────────────────────────


def test_create_and_assign_signature() -> None:
    """create_and_assign has the expected parameter names and defaults."""
    sig = inspect.signature(task_registry.create_and_assign)  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
    params = sig.parameters
    assert list(params.keys()) == [
        "title",
        "description",
        "preset",
        "label",
        "config_overlay",
        "machine",
        "parent",
        "remind_interval_seconds",
        "priority",
    ]
    assert params["preset"].default == "coder"
    assert params["label"].default is None
    assert params["config_overlay"].default is None
    assert params["machine"].default is None
    assert params["parent"].default is inspect.Parameter.empty  # required, no default
    assert params["remind_interval_seconds"].default is None
    assert params["priority"].default == "P2"


def test_create_and_assign_returns_task_and_agent_id(
    db_conn: psycopg.Connection, root_task_id: int
) -> None:
    """create_and_assign returns a (Task, agent_id) tuple."""
    agent_id = _seed_agent(db_conn)
    spawned_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        with (
            patch("ava.agents.spawn", return_value=spawned_id) as mock_spawn,
            patch("ava.agents.send_message"),
        ):
            task, aid = task_registry.create_and_assign("title", "description", parent=root_task_id)  # pyright: ignore[reportUnknownMemberType]
        assert isinstance(task, task_registry.Task)
        assert task.title == "title"
        assert task.description == "description"
        assert aid == spawned_id
        mock_spawn.assert_called_once()
    finally:
        ava._boot._agent_id = original


def test_create_and_assign_task_owned_by_spawned_agent(
    db_conn: psycopg.Connection, root_task_id: int
) -> None:
    """After create_and_assign, the task's owner is the spawned agent id."""
    agent_id = _seed_agent(db_conn)
    spawned_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        with patch("ava.agents.spawn", return_value=spawned_id), patch("ava.agents.send_message"):
            task, _ = task_registry.create_and_assign("title", "description", parent=root_task_id)  # pyright: ignore[reportUnknownMemberType]
        assert task.owner == spawned_id
        assert _persisted_owner(db_conn, task.id) == spawned_id
    finally:
        ava._boot._agent_id = original


def test_create_and_assign_passes_spawn_args(
    db_conn: psycopg.Connection, root_task_id: int
) -> None:
    """create_and_assign forwards preset, label, config_overlay to spawn."""
    agent_id = _seed_agent(db_conn)
    spawned_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        with (
            patch("ava.agents.spawn", return_value=spawned_id) as mock_spawn,
            patch("ava.agents.send_message"),
        ):
            task_registry.create_and_assign(  # pyright: ignore[reportUnknownMemberType]
                "title",
                "description",
                preset="researcher",
                label="test-label",
                config_overlay={"llm_model": "fast"},
                machine="test-machine",
                parent=root_task_id,
            )
        mock_spawn.assert_called_once_with(
            preset="researcher",
            label="test-label",
            config_overlay={"llm_model": "fast"},
            machine="test-machine",
        )
    finally:
        ava._boot._agent_id = original


def test_create_and_assign_sends_notification(
    db_conn: psycopg.Connection, root_task_id: int
) -> None:
    """create_and_assign triggers a notification to the spawned agent via create()."""
    agent_id = _seed_agent(db_conn)
    spawned_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        with (
            patch("ava.agents.spawn", return_value=spawned_id),
            patch("ava.agents.send_message") as mock_send,
        ):
            task, _ = task_registry.create_and_assign(  # pyright: ignore[reportUnknownMemberType]
                "my title", "my description", parent=root_task_id
            )
        # create(owner=spawned_id) calls _notify_owner_change → send_message
        mock_send.assert_called_once()
        call_args = mock_send.call_args
        assert call_args[0][0] == spawned_id
        msg = call_args[0][1]
        assert f"Task #{task.id}" in msg
        assert "my title" in msg
        assert "my description" in msg
        assert "assigned to you" in msg
    finally:
        ava._boot._agent_id = original


def test_create_and_assign_no_notification_when_spawn_fails(
    db_conn: psycopg.Connection, root_task_id: int
) -> None:
    """If spawn raises, no task is created and no notification is sent."""
    agent_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        with (
            patch("ava.agents.spawn", side_effect=RuntimeError("spawn failed")),
            patch("ava.agents.send_message") as mock_send,
            pytest.raises(RuntimeError, match="spawn failed"),
        ):
            task_registry.create_and_assign("title", "description", parent=root_task_id)  # pyright: ignore[reportUnknownMemberType]
        mock_send.assert_not_called()
    finally:
        ava._boot._agent_id = original


def test_create_and_assign_honours_parent(db_conn: psycopg.Connection, root_task_id: int) -> None:
    """create_and_assign passes parent through to create."""
    agent_id = _seed_agent(db_conn)
    spawned_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        parent_task = task_registry.create("parent", "detail", parent=root_task_id)
        with patch("ava.agents.spawn", return_value=spawned_id), patch("ava.agents.send_message"):
            task, _ = task_registry.create_and_assign("child", "detail", parent=parent_task.id)  # pyright: ignore[reportUnknownMemberType]
        assert task.parent_id == parent_task.id
    finally:
        ava._boot._agent_id = original


def test_create_and_assign_honours_remind_interval_seconds(
    db_conn: psycopg.Connection, root_task_id: int
) -> None:
    """create_and_assign passes remind_interval_seconds through to create."""
    agent_id = _seed_agent(db_conn)
    spawned_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        with patch("ava.agents.spawn", return_value=spawned_id), patch("ava.agents.send_message"):
            task, _ = task_registry.create_and_assign(  # pyright: ignore[reportUnknownMemberType]
                "title", "detail", remind_interval_seconds=3600, parent=root_task_id
            )
        assert task.remind_interval_seconds == 3600
    finally:
        ava._boot._agent_id = original


def test_create_and_assign_honours_priority(db_conn: psycopg.Connection, root_task_id: int) -> None:
    """create_and_assign passes priority through to create."""
    agent_id = _seed_agent(db_conn)
    spawned_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        with patch("ava.agents.spawn", return_value=spawned_id), patch("ava.agents.send_message"):
            task, _ = task_registry.create_and_assign(  # pyright: ignore[reportUnknownMemberType]
                "title", "detail", priority="P0", parent=root_task_id
            )
        assert task.priority == "P0"
        assert _persisted_priority(db_conn, task.id) == "P0"
    finally:
        ava._boot._agent_id = original


def test_create_and_assign_remind_interval_none(
    db_conn: psycopg.Connection, root_task_id: int
) -> None:
    """create_and_assign with remind_interval_seconds=None falls back to the default —
    reminders cannot be disabled."""
    agent_id = _seed_agent(db_conn)
    spawned_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        with patch("ava.agents.spawn", return_value=spawned_id), patch("ava.agents.send_message"):
            task, _ = task_registry.create_and_assign(  # pyright: ignore[reportUnknownMemberType]
                "title", "detail", remind_interval_seconds=None, parent=root_task_id
            )
        assert task.remind_interval_seconds == 7200  # P2 default -> 2h
    finally:
        ava._boot._agent_id = original


def test_create_and_assign_uses_default_preset(
    db_conn: psycopg.Connection, root_task_id: int
) -> None:
    """When no preset is given, 'coder' is the default."""
    agent_id = _seed_agent(db_conn)
    spawned_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        with (
            patch("ava.agents.spawn", return_value=spawned_id) as mock_spawn,
            patch("ava.agents.send_message"),
        ):
            task_registry.create_and_assign("title", "description", parent=root_task_id)  # pyright: ignore[reportUnknownMemberType]
        mock_spawn.assert_called_once_with(
            preset="coder", label=None, config_overlay=None, machine=None
        )
    finally:
        ava._boot._agent_id = original


def test_create_and_assign_rejects_bad_parent_before_spawn(
    db_conn: psycopg.Connection, root_task_id: int
) -> None:
    """A missing parent is rejected before the agent spawns, so no orphaned
    agent is left behind."""
    agent_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        with (
            patch("ava.agents.spawn") as mock_spawn,
            pytest.raises(ValueError, match="parent task 999999 does not exist"),
        ):
            task_registry.create_and_assign(  # pyright: ignore[reportUnknownMemberType]
                "orphan-child", "d", parent=999_999
            )
        mock_spawn.assert_not_called()
    finally:
        ava._boot._agent_id = original


# ── create() — duplicate title rejection ──────────────────────────────────


def test_create_duplicate_open_title_raises(db_conn, root_task_id: int):
    """Creating a task with the same title as an open task raises ValueError."""
    agent_id = _seed_agent(db_conn)  # pyright: ignore[reportUnknownArgumentType]
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        task_registry.create("unique title", "detail", parent=root_task_id)
        with pytest.raises(ValueError, match="already exists"):
            task_registry.create("unique title", "detail", parent=root_task_id)
    finally:
        ava._boot._agent_id = original


def test_create_duplicate_in_progress_title_raises(db_conn, root_task_id: int):
    """Creating a task with the same title as an in_progress task raises ValueError."""
    agent_id = _seed_agent(db_conn)  # pyright: ignore[reportUnknownArgumentType]
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        task = task_registry.create("unique title 2", "detail", parent=root_task_id)
        task_registry.update(task.id, status="in_progress")
        with pytest.raises(ValueError, match="already exists"):
            task_registry.create("unique title 2", "detail", parent=root_task_id)
    finally:
        ava._boot._agent_id = original


def test_create_same_title_done_allowed(db_conn, root_task_id: int):
    """Creating a task with the same title as a done task is allowed."""
    agent_id = _seed_agent(db_conn)  # pyright: ignore[reportUnknownArgumentType]
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        task = task_registry.create("unique title 3", "detail", parent=root_task_id)
        task_registry.update(task.id, status="done")
        task2 = task_registry.create("unique title 3", "detail", parent=root_task_id)
        assert task2.id != task.id
    finally:
        ava._boot._agent_id = original


# ── database-level constraints (migration agent-tasks-constraints) ─────────


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


def test_schema_unique_open_title_backstop(db_conn):
    """The partial unique index rejects a second open/in_progress task with a
    duplicate title, but allows the title once the earlier task leaves
    open/in_progress — the database mirror of the app-level rule."""
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


def test_create_unique_violation_race_becomes_value_error(db_conn, root_task_id: int):
    """When two creates race past the app-level pre-check, the partial unique
    index rejects the second INSERT with UniqueViolation; create() translates
    that into the same ValueError agents see on the normal path."""
    agent_id = _seed_agent(db_conn)  # pyright: ignore[reportUnknownArgumentType]
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        task_registry.create("raced-title", "d", parent=root_task_id)
        real_execute = psycopg.Cursor.execute
        with (
            patch.object(psycopg.Cursor, "execute", _fake_no_duplicate_precheck(real_execute)),  # pyright: ignore[reportUnknownArgumentType]
            pytest.raises(ValueError, match="already exists"),
        ):
            task_registry.create("raced-title", "d", parent=root_task_id)
    finally:
        ava._boot._agent_id = original


def test_update_rename_race_becomes_value_error(db_conn, root_task_id: int):
    """Same backstop on the rename path: a concurrent rename that lands
    between update()'s pre-check and its UPDATE surfaces as ValueError, not a
    raw psycopg error."""
    agent_id = _seed_agent(db_conn)  # pyright: ignore[reportUnknownArgumentType]
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
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
    finally:
        ava._boot._agent_id = original


# ── update() — title rename ────────────────────────────────────────────────


def test_update_title_renames(db_conn, root_task_id: int):
    agent_id = _seed_agent(db_conn)  # pyright: ignore[reportUnknownArgumentType]
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        task = task_registry.create("old title", "detail", parent=root_task_id)
        task_registry.update(task.id, title="new title")
        assert task_registry.get(task.id).title == "new title"
    finally:
        ava._boot._agent_id = original


def test_update_title_duplicate_open_raises(db_conn, root_task_id: int):
    """Renaming to another open/in_progress task's title raises ValueError —
    the same invariant create() enforces."""
    agent_id = _seed_agent(db_conn)  # pyright: ignore[reportUnknownArgumentType]
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        task_registry.create("taken title", "detail", parent=root_task_id)
        task = task_registry.create("free title", "detail", parent=root_task_id)
        with pytest.raises(ValueError, match="already exists"):
            task_registry.update(task.id, title="taken title")
        assert task_registry.get(task.id).title == "free title"
    finally:
        ava._boot._agent_id = original


def test_update_title_reassign_notifies_with_new_title(db_conn, root_task_id: int):
    """A rename and a reassignment in one update() notify with the new title."""
    agent_id = _seed_agent(db_conn)  # pyright: ignore[reportUnknownArgumentType]
    other_id = _seed_agent(db_conn)  # pyright: ignore[reportUnknownArgumentType]
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        task = task_registry.create("old title", "detail", parent=root_task_id)
        with patch("ava.agents.send_message") as mock_send:
            task_registry.update(task.id, title="new title", owner=other_id)
        assert any("new title" in call.args[1] for call in mock_send.call_args_list)
    finally:
        ava._boot._agent_id = original


def test_create_same_title_cancelled_allowed(db_conn, root_task_id: int):
    """Creating a task with the same title as a cancelled task is allowed."""
    agent_id = _seed_agent(db_conn)  # pyright: ignore[reportUnknownArgumentType]
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        task = task_registry.create("unique title 4", "detail", parent=root_task_id)
        task_registry.update(task.id, status="cancelled")
        task2 = task_registry.create("unique title 4", "detail", parent=root_task_id)
        assert task2.id != task.id
    finally:
        ava._boot._agent_id = original


def test_create_unique_title_no_conflict(db_conn, root_task_id: int):
    """Creating a task with a truly unique title succeeds."""
    agent_id = _seed_agent(db_conn)  # pyright: ignore[reportUnknownArgumentType]
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        task1 = task_registry.create("title a", "detail", parent=root_task_id)
        task2 = task_registry.create("title b", "detail", parent=root_task_id)
        assert task1.id != task2.id
    finally:
        ava._boot._agent_id = original


# ── update() — note parameter ────────────────────────────────────────────


def test_update_note_with_cancel_appends_to_results(
    db_conn: psycopg.Connection, root_task_id: int
) -> None:
    """note with status='cancelled' appends a timestamped line to results."""
    agent_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        task = task_registry.create("title", "detail", parent=root_task_id)
        task_registry.log(task.id, "work started")
        task_registry.update(task.id, status="cancelled", note="no longer needed")
        results = task_registry.get(task.id).results
        assert results is not None
        lines = results.splitlines()
        assert len(lines) == 2
        assert lines[0].endswith("work started")
        assert lines[1].endswith("no longer needed")
        assert re.match(r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] ", lines[1])
    finally:
        ava._boot._agent_id = original


def test_update_note_with_done_appends_to_results(
    db_conn: psycopg.Connection, root_task_id: int
) -> None:
    """note works with any status, not just cancelled."""
    agent_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        task = task_registry.create("title", "detail", parent=root_task_id)
        task_registry.update(task.id, status="done", note="all tests pass")
        results = task_registry.get(task.id).results
        assert results is not None
        assert "all tests pass" in results
    finally:
        ava._boot._agent_id = original


def test_update_note_standalone_no_status_change(
    db_conn: psycopg.Connection, root_task_id: int
) -> None:
    """note alone (no status change) works as a drop-in for log()."""
    agent_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        task = task_registry.create("title", "detail", parent=root_task_id)
        task_registry.update(task.id, note="progress update")
        results = task_registry.get(task.id).results
        assert results is not None
        assert "progress update" in results
        # Status unchanged
        assert task_registry.get(task.id).status == "open"
    finally:
        ava._boot._agent_id = original


def test_update_note_with_no_prior_results(db_conn: psycopg.Connection, root_task_id: int) -> None:
    """When a task has no prior results, the note line is the only content."""
    agent_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        task = task_registry.create("title", "detail", parent=root_task_id)
        assert task.results is None
        task_registry.update(task.id, status="cancelled", note="duplicate")
        results = task_registry.get(task.id).results
        assert results is not None
        assert "duplicate" in results
    finally:
        ava._boot._agent_id = original


def test_update_note_with_results_overwrite_appends_after(
    db_conn: psycopg.Connection, root_task_id: int
) -> None:
    """When results is also set, the note is appended after the new results."""
    agent_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        task = task_registry.create("title", "detail", parent=root_task_id)
        task_registry.update(
            task.id, status="cancelled", results="final notes", note="out of scope"
        )
        results = task_registry.get(task.id).results
        assert results is not None
        lines = results.splitlines()
        assert lines[0] == "final notes"
        assert lines[1].endswith("out of scope")
    finally:
        ava._boot._agent_id = original


def test_update_note_none_is_noop(db_conn: psycopg.Connection, root_task_id: int) -> None:
    """note=None does nothing — no line appended."""
    agent_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        task = task_registry.create("title", "detail", parent=root_task_id)
        task_registry.update(task.id, status="cancelled", note=None)
        results = task_registry.get(task.id).results
        # None because no note was appended and no prior results existed
        assert results is None
    finally:
        ava._boot._agent_id = original


def test_log_delegates_to_update_note(db_conn: psycopg.Connection, root_task_id: int) -> None:
    """log() is a thin wrapper around update(note=...)."""
    agent_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        task = task_registry.create("title", "detail", parent=root_task_id)
        task_registry.log(task.id, "via log()")
        results = task_registry.get(task.id).results
        assert results is not None
        assert "via log()" in results
        # Status unchanged
        assert task_registry.get(task.id).status == "open"
    finally:
        ava._boot._agent_id = original


def test_update_root_task_is_rejected(db_conn: psycopg.Connection, root_task_id: int) -> None:
    """The system root task is immutable: any update() targeting it fails fast
    and leaves the row untouched."""
    agent_id = _seed_agent(db_conn)
    root_id = root_task_id
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        with pytest.raises(ValueError, match="root task"):
            task_registry.update(root_id, status="done")
        with pytest.raises(ValueError, match="root task"):
            task_registry.update(root_id, owner=agent_id)
        # The rejected writes never landed — the root row is unchanged.
        root = task_registry.get(root_id)
        assert root.status == "in_progress"
        assert root.owner is None
    finally:
        ava._boot._agent_id = original


# ── parent requirement & tree anchoring ──────────────────────────────────────


def _parent_of(db: psycopg.Connection, task_id: int) -> int | None:
    with db.cursor() as cur:
        cur.execute("SELECT parent_id FROM agent_tasks WHERE id = %s", (task_id,))
        row = cur.fetchone()
    assert row is not None
    return row[0]


def test_create_requires_parent(db_conn: psycopg.Connection, root_task_id: int) -> None:
    """`parent` is a required keyword-only argument — calling create() without
    it fails at the signature, so no task can silently land on the root."""
    agent_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        with pytest.raises(TypeError, match="missing 1 required keyword-only argument: 'parent'"):
            task_registry.create("no-parent", "detail")  # pyright: ignore[reportCallIssue]
    finally:
        ava._boot._agent_id = original


def test_create_with_root_parent_anchors_to_root(
    db_conn: psycopg.Connection, root_task_id: int
) -> None:
    """A top-level task passes the system root task's id as its parent. The
    root itself is never made its own parent (it stays parent-less)."""
    agent_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        task = task_registry.create("anchored-to-root", "detail", parent=root_task_id)
        assert task.parent_id == root_task_id
        # The persisted row agrees, and the root did not gain a parent.
        assert _parent_of(db_conn, task.id) == root_task_id
        assert _parent_of(db_conn, root_task_id) is None
    finally:
        ava._boot._agent_id = original


def test_create_with_explicit_parent(db_conn: psycopg.Connection, root_task_id: int) -> None:
    """A subtask passes the id of an existing task as its parent; the parent
    itself is top-level (parented by the system root)."""
    agent_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        parent = task_registry.create("explicit-parent", "detail", parent=root_task_id)
        child = task_registry.create("explicit-child", "detail", parent=parent.id)
        assert parent.parent_id == root_task_id  # top-level task under the root
        assert child.parent_id == parent.id  # subtask under its parent
    finally:
        ava._boot._agent_id = original


def test_create_rejects_missing_parent(db_conn: psycopg.Connection, root_task_id: int) -> None:
    """A parent id that names no existing task is rejected with a friendly
    ValueError instead of a raw foreign-key violation."""
    agent_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        with pytest.raises(ValueError, match="parent task 999999 does not exist"):
            task_registry.create("orphan", "detail", parent=999_999)
    finally:
        ava._boot._agent_id = original


def test_create_rejects_parent_1_when_not_root(db_conn: psycopg.Connection) -> None:
    """The documented root id (1) is enforced: on a deployment where task 1 is
    not the system root, create(parent=1) fails loudly instead of silently
    attaching a top-level task under a different parent."""
    agent_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        # Self-contained: pin task 1 (non-root) and task 2 (the root), so the
        # assertion does not depend on the fixture-seeded root's id.
        with db_conn.cursor() as cur:
            cur.execute("DELETE FROM agent_tasks")
            cur.execute(
                "INSERT INTO agent_tasks (id, title, description, status, created_by, owner, is_root) "
                "VALUES (1, 'not-root', 'd', 'open', %s, %s, FALSE), "
                "(2, 'Root', 'root', 'in_progress', 'system', NULL, TRUE)",
                (str(agent_id), agent_id),
            )
        db_conn.commit()
        with pytest.raises(ValueError, match="not the system root task"):
            task_registry.create("wants-root", "d", parent=1)
        # The actual root id works as a top-level parent.
        task = task_registry.create("top-level", "d", parent=2)
        assert task.parent_id == 2
    finally:
        ava._boot._agent_id = original


def _migration_sql(name_suffix: str) -> tuple[str, str]:
    """Return (up_sql, down_sql) for the post-baseline migration whose kebab name
    ends with `name_suffix`, located by its timestamp-prefixed filename."""
    mig_dir = Path(__file__).resolve().parents[2] / "migrations"
    up = next(mig_dir.glob(f"*_{name_suffix}.sql"))
    down = next(mig_dir.glob(f"*_{name_suffix}.down.sql"))
    return up.read_text(encoding="utf-8"), down.read_text(encoding="utf-8")


def _persisted_parent(db: psycopg.Connection, task_id: int) -> int | None:
    with db.cursor() as cur:
        cur.execute("SELECT parent_id FROM agent_tasks WHERE id = %s", (task_id,))
        row = cur.fetchone()
    assert row is not None
    return row[0]


def test_update_reparents_under_new_parent(db_conn: psycopg.Connection, root_task_id: int) -> None:
    agent_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        parent = task_registry.create("parent-task", "d", parent=root_task_id)
        child = task_registry.create("child-task", "d", parent=root_task_id)
        # Both tasks are top-level (parent=root); reparent the child under `parent`.
        task_registry.update(child.id, parent_id=parent.id)
        assert _persisted_parent(db_conn, child.id) == parent.id
    finally:
        ava._boot._agent_id = original


def test_update_parent_none_moves_to_root(db_conn: psycopg.Connection, root_task_id: int) -> None:
    agent_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        parent = task_registry.create("parent-task2", "d", parent=root_task_id)
        child = task_registry.create("child-task2", "d", parent=root_task_id)
        task_registry.update(child.id, parent_id=parent.id)
        assert _persisted_parent(db_conn, child.id) == parent.id
        # Explicit None = back under the system root (the same anchor create()
        # callers pass explicitly for top-level tasks).
        task_registry.update(child.id, parent_id=None)
        assert _persisted_parent(db_conn, child.id) == root_task_id
    finally:
        ava._boot._agent_id = original


def test_update_rejects_self_parent(db_conn: psycopg.Connection, root_task_id: int) -> None:
    agent_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        task = task_registry.create("self-parent", "d", parent=root_task_id)
        with pytest.raises(ValueError, match="own parent"):
            task_registry.update(task.id, parent_id=task.id)
    finally:
        ava._boot._agent_id = original


def test_update_rejects_missing_parent(db_conn: psycopg.Connection, root_task_id: int) -> None:
    agent_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        task = task_registry.create("missing-parent", "d", parent=root_task_id)
        with pytest.raises(ValueError, match="does not exist"):
            task_registry.update(task.id, parent_id=999_999)
    finally:
        ava._boot._agent_id = original


def test_update_rejects_cycle_under_own_descendant(
    db_conn: psycopg.Connection, root_task_id: int
) -> None:
    agent_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        a = task_registry.create("cycle-a", "d", parent=root_task_id)
        b = task_registry.create("cycle-b", "d", parent=root_task_id)
        c = task_registry.create("cycle-c", "d", parent=root_task_id)
        task_registry.update(b.id, parent_id=a.id)
        task_registry.update(c.id, parent_id=b.id)
        # a -> c would put a under its own descendant: rejected.
        with pytest.raises(ValueError, match="descendant"):
            task_registry.update(a.id, parent_id=c.id)
        # The tree is unchanged.
        assert _persisted_parent(db_conn, b.id) == a.id
        assert _persisted_parent(db_conn, c.id) == b.id
    finally:
        ava._boot._agent_id = original


def test_update_parent_alone_is_a_valid_update(
    db_conn: psycopg.Connection, root_task_id: int
) -> None:
    """A pure reparent (no other field) must not hit the nothing-to-update guard."""
    agent_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        parent = task_registry.create("lone-parent", "d", parent=root_task_id)
        child = task_registry.create("lone-child", "d", parent=root_task_id)
        task_registry.update(child.id, parent_id=parent.id)
        assert _persisted_parent(db_conn, child.id) == parent.id
    finally:
        ava._boot._agent_id = original


def test_sdk_task_timestamps_are_bare_cluster_zone(
    db_conn: psycopg.Connection, root_task_id: int
) -> None:
    """Issue #181: the SDK task object must not mix timestamp conventions.

    `created_at` / `updated_at` / `last_reminded_at` are agent-facing rendered
    timestamps, so they carry the bare cluster-zone form (no UTC/offset suffix)
    — the same convention as the `results` notes. The gateway JSON API and the
    DB keep explicit offsets; only this object is uniform."""
    agent_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        task = task_registry.create("ts-uniform", "d", parent=root_task_id)
        task_registry.log(task.id, "probe log line 1")
        got = task_registry.get(task.id)

        for field in ("created_at", "updated_at"):
            value = getattr(got, field)
            assert isinstance(value, str), f"{field} must be a rendered string"
            # Bare cluster-zone: bracket format, no suffix of any kind.
            assert re.match(r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\]$", value), (
                f"{field} is not a bare timestamp: {value!r}"
            )
            assert "+00:00" not in value and "+08" not in value and "Z" not in value

        # last_reminded_at is None before any reminder (still typed as rendered
        # string once set — same convention).
        assert got.last_reminded_at is None

        # The results notes use the same bare convention — one object, one
        # convention end to end.
        assert got.results is not None
        for line in got.results.splitlines():
            assert re.match(r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] ", line), (
                f"note line is not bare: {line!r}"
            )
    finally:
        ava._boot._agent_id = original
