"""`plugins.ava_fleet.task_registry.create` — the default reminder interval.

A new task reminds its owner after 30 minutes of silence. `create()` defaults
`remind_interval_seconds` to 1800s; an explicit value (positive, <= 24h) is honoured.
Reminders cannot be disabled — an explicit `None` falls back to the default.
Persisted via `ava.DB`.
"""

from __future__ import annotations

import inspect
import re
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


def test_default_remind_interval_is_30_min() -> None:
    assert (
        inspect.signature(task_registry.create).parameters["remind_interval_seconds"].default
        == 1800
    )


def test_create_defaults_to_30_min(db_conn: psycopg.Connection) -> None:
    agent_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        task = task_registry.create("title", "detail")
        assert task.remind_interval_seconds == 1800
        assert _persisted_remind_interval_seconds(db_conn, task.id) == 1800
    finally:
        ava._boot._agent_id = original


def test_create_honours_explicit_value(db_conn: psycopg.Connection) -> None:
    agent_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        task = task_registry.create("title", "detail", remind_interval_seconds=3600)
        assert task.remind_interval_seconds == 3600
        assert _persisted_remind_interval_seconds(db_conn, task.id) == 3600
    finally:
        ava._boot._agent_id = original


def test_create_none_falls_back_to_default(db_conn: psycopg.Connection) -> None:
    """Reminders cannot be disabled: create(remind_interval_seconds=None) uses the
    default rather than writing NULL."""
    agent_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        task = task_registry.create("title", "detail", remind_interval_seconds=None)
        assert task.remind_interval_seconds == 1800
        assert _persisted_remind_interval_seconds(db_conn, task.id) == 1800
    finally:
        ava._boot._agent_id = original


def test_create_rejects_non_positive_interval(db_conn: psycopg.Connection) -> None:
    agent_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        with pytest.raises(ValueError, match="positive number of seconds"):
            task_registry.create("title", "detail", remind_interval_seconds=0)
    finally:
        ava._boot._agent_id = original


def test_create_rejects_interval_over_24h(db_conn: psycopg.Connection) -> None:
    agent_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        with pytest.raises(ValueError, match="cannot be disabled"):
            task_registry.create("title", "detail", remind_interval_seconds=86401)
    finally:
        ava._boot._agent_id = original


def test_create_honours_24h_boundary(db_conn: psycopg.Connection) -> None:
    """Exactly 24h (86400s) is the largest accepted interval."""
    agent_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        task = task_registry.create("title", "detail", remind_interval_seconds=86400)
        assert task.remind_interval_seconds == 86400
    finally:
        ava._boot._agent_id = original


def test_update_changes_remind_interval_seconds(db_conn: psycopg.Connection) -> None:
    agent_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        task = task_registry.create("title", "detail")
        assert task.remind_interval_seconds == 1800
        task_registry.update(task.id, remind_interval_seconds=7200)
        assert _persisted_remind_interval_seconds(db_conn, task.id) == 7200
    finally:
        ava._boot._agent_id = original


def test_update_none_remind_interval_is_noop(db_conn: psycopg.Connection) -> None:
    """remind_interval_seconds=None means "no change" (reminders cannot be disabled),
    not "write NULL"; alongside a real change it leaves the interval intact."""
    agent_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        task = task_registry.create("title", "detail")
        task_registry.update(task.id, status="in_progress", remind_interval_seconds=None)
        assert _persisted_remind_interval_seconds(db_conn, task.id) == 1800
    finally:
        ava._boot._agent_id = original


def test_update_rejects_interval_over_24h(db_conn: psycopg.Connection) -> None:
    agent_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        task = task_registry.create("title", "detail")
        with pytest.raises(ValueError, match="cannot be disabled"):
            task_registry.update(task.id, remind_interval_seconds=86401)
    finally:
        ava._boot._agent_id = original


def test_update_resets_reminder_count(db_conn: psycopg.Connection) -> None:
    agent_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        task = task_registry.create("title", "detail")
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


def test_update_description(db_conn: psycopg.Connection) -> None:
    agent_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        task = task_registry.create("title", "detail")
        task_registry.update(task.id, description="revised detail")
        assert task_registry.get(task.id).description == "revised detail"
    finally:
        ava._boot._agent_id = original


def test_update_nothing_raises(db_conn: psycopg.Connection) -> None:
    agent_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        task = task_registry.create("title", "detail")
        with pytest.raises(ValueError, match="at least one"):
            task_registry.update(task.id)
    finally:
        ava._boot._agent_id = original


def test_log_appends_timestamped_lines(db_conn: psycopg.Connection) -> None:
    agent_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        task = task_registry.create("title", "detail")
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
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
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
            task = task_registry.create(f"stamped in {tz}", "detail")
            task_registry.log(task.id, "note")
            results = task_registry.get(task.id).results
            assert results is not None
            stamps[tz] = results.split("]")[0]
        # Honolulu is UTC-10 year-round, Shanghai UTC+8: 18 hours apart, so two
        # stamps taken seconds apart cannot agree unless the setting is read.
        assert stamps["Asia/Shanghai"] != stamps["Pacific/Honolulu"]
    finally:
        ava._boot._agent_id = original


def test_log_preserves_replaced_results(db_conn: psycopg.Connection) -> None:
    agent_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        task = task_registry.create("title", "detail")
        task_registry.update(task.id, results="prior log without newline")
        task_registry.log(task.id, "appended")
        results = task_registry.get(task.id).results
        assert results is not None
        lines = results.splitlines()
        assert lines[0] == "prior log without newline"
        assert lines[1].endswith("appended")
    finally:
        ava._boot._agent_id = original


def test_log_resets_reminder_count(db_conn: psycopg.Connection) -> None:
    agent_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        task = task_registry.create("title", "detail")
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


def test_deprecated_aliases_still_work(db_conn: psycopg.Connection) -> None:
    agent_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        task = task_registry.create("title", brief="via alias")
        assert task.description == "via alias"
        assert task.brief == "via alias"
        task_registry.update(task.id, content="log via alias")
        got = task_registry.get(task.id)
        assert got.results == "log via alias"
        assert got.content == "log via alias"
    finally:
        ava._boot._agent_id = original


def test_alias_and_new_name_together_raise(db_conn: psycopg.Connection) -> None:
    agent_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        with pytest.raises(TypeError, match="deprecated alias"):
            task_registry.create("title", "detail", brief="also detail")
        task = task_registry.create("title", "detail")
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


def test_create_default_owner_is_creator(db_conn: psycopg.Connection) -> None:
    """When owner is not passed, the creating agent is the owner."""
    agent_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        task = task_registry.create("title", "detail")
        assert task.owner == agent_id
        assert _persisted_owner(db_conn, task.id) == agent_id
    finally:
        ava._boot._agent_id = original


def test_create_explicit_owner(db_conn: psycopg.Connection) -> None:
    """When owner is explicitly set, that agent becomes the owner."""
    agent_id = _seed_agent(db_conn)
    other_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        with patch("ava.agents.send_message"):
            task = task_registry.create("title", "detail", owner=other_id)
        assert task.owner == other_id
        assert _persisted_owner(db_conn, task.id) == other_id
    finally:
        ava._boot._agent_id = original


def test_create_with_owner_notifies_target(db_conn: psycopg.Connection) -> None:
    """When owner != creator, a notification message is sent to the owner."""
    agent_id = _seed_agent(db_conn)
    other_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        with patch("ava.agents.send_message") as mock_send:
            task = task_registry.create("title", "detail", owner=other_id)
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


def test_create_with_owner_self_no_notification(db_conn: psycopg.Connection) -> None:
    """When owner == creator, no notification is sent."""
    agent_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        with patch("ava.agents.send_message") as mock_send:
            task_registry.create("title", "detail", owner=agent_id)
            mock_send.assert_not_called()
    finally:
        ava._boot._agent_id = original


def test_create_without_owner_no_notification(db_conn: psycopg.Connection) -> None:
    """When owner is not passed (default = creator), no notification is sent."""
    agent_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        with patch("ava.agents.send_message") as mock_send:
            task_registry.create("title", "detail")
            mock_send.assert_not_called()
    finally:
        ava._boot._agent_id = original


# ── update() — owner semantics ────────────────────────────────────────────


def test_update_owner_reassign_notifies(db_conn: psycopg.Connection) -> None:
    """When owner changes to a different agent, the new owner is notified.
    The old owner is skipped when they are the actor performing the update."""
    agent_id = _seed_agent(db_conn)
    other_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        task = task_registry.create("title", "detail")
        with patch("ava.agents.send_message") as mock_send:
            task_registry.update(task.id, owner=other_id)
            # Only new owner notified; old owner == actor is skipped
            mock_send.assert_called_once()
            call_args = mock_send.call_args
            assert call_args[0][0] == other_id
            assert "now assigned" in call_args[0][1]
    finally:
        ava._boot._agent_id = original


def test_update_owner_none_is_noop(db_conn: psycopg.Connection) -> None:
    """owner=None means do not change, not release. Raises ValueError
    because nothing else is changing either."""
    agent_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        task = task_registry.create("title", "detail")
        with pytest.raises(ValueError, match="at least one"):
            task_registry.update(task.id, owner=None)
        # Owner should be unchanged
        assert _persisted_owner(db_conn, task.id) == agent_id
    finally:
        ava._boot._agent_id = original


def test_update_owner_none_with_status_is_noop_for_owner(db_conn: psycopg.Connection) -> None:
    """owner=None alongside a real change (status) only changes status, not owner."""
    agent_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        task = task_registry.create("title", "detail")
        task_registry.update(task.id, status="in_progress", owner=None)
        got = task_registry.get(task.id)
        assert got.status == "in_progress"
        assert got.owner == agent_id  # unchanged
    finally:
        ava._boot._agent_id = original


def test_update_owner_self_no_notification(db_conn: psycopg.Connection) -> None:
    """Reassigning to yourself is a no-op notification-wise."""
    agent_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        task = task_registry.create("title", "detail")
        with patch("ava.agents.send_message") as mock_send:
            task_registry.update(task.id, owner=agent_id)
            # send_message should not be called because old_owner == new_owner
            mock_send.assert_not_called()
    finally:
        ava._boot._agent_id = original


def test_update_owner_new_terminated_still_notified(db_conn: psycopg.Connection) -> None:
    """A terminated new owner IS notified — send_message auto-resurrects it, so
    an assigned task never strands on a dead agent."""
    agent_id = _seed_agent(db_conn)
    dead_id = _seed_agent(db_conn, status="terminated")
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        task = task_registry.create("title", "detail")
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


def test_update_non_owner_notifies_owner(db_conn: psycopg.Connection) -> None:
    """A non-owner update (here: the motivating cancel case) notifies the owner
    with the task, the changed fields, and the author."""
    actor_id = _seed_agent(db_conn)
    owner_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = actor_id
    try:
        with patch("ava.agents.send_message"):
            task = task_registry.create("title", "detail", owner=owner_id)
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


def test_update_by_owner_no_notification(db_conn: psycopg.Connection) -> None:
    """The owner updating its own task is not notified about its own action."""
    agent_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        task = task_registry.create("title", "detail")
        with patch("ava.agents.send_message") as mock_send:
            task_registry.update(task.id, status="done", results="shipped")
            mock_send.assert_not_called()
    finally:
        ava._boot._agent_id = original


def test_update_non_owner_notifies_terminated_owner(db_conn: psycopg.Connection) -> None:
    """A terminated owner is still told — send_message auto-resurrects it, so an
    owner learns its task changed even while it is down (same rule as the
    new-owner leg of a reassignment)."""
    actor_id = _seed_agent(db_conn)
    dead_owner = _seed_agent(db_conn, status="terminated")
    original = ava._boot._agent_id
    ava._boot._agent_id = actor_id
    try:
        with patch("ava.agents.send_message"):
            task = task_registry.create("title", "detail", owner=dead_owner)
        with patch("ava.agents.send_message") as mock_send:
            task_registry.update(task.id, status="done")
            mock_send.assert_called_once()
            assert mock_send.call_args[0][0] == dead_owner
            assert "was updated by" in mock_send.call_args[0][1]
    finally:
        ava._boot._agent_id = original


def test_update_owner_change_appends_changes_to_new_owner(db_conn: psycopg.Connection) -> None:
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
            task = task_registry.create("title", "detail", owner=old_owner)
        with patch("ava.agents.send_message") as mock_send:
            task_registry.update(task.id, status="cancelled", owner=new_owner)
            assert mock_send.call_count == 2
            msgs = {call.args[0]: call.args[1] for call in mock_send.call_args_list}
            assert "now assigned" in msgs[new_owner]
            assert "status → cancelled" in msgs[new_owner]
            assert "no longer assigned" in msgs[old_owner]
    finally:
        ava._boot._agent_id = original


def test_log_by_non_owner_notifies_owner(db_conn: psycopg.Connection) -> None:
    """log() by a non-owner counts as a write: the owner hears about it too."""
    actor_id = _seed_agent(db_conn)
    owner_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = actor_id
    try:
        with patch("ava.agents.send_message"):
            task = task_registry.create("title", "detail", owner=owner_id)
        with patch("ava.agents.send_message") as mock_send:
            task_registry.log(task.id, "progress update")
            mock_send.assert_called_once()
            assert mock_send.call_args[0][0] == owner_id
            assert "note appended" in mock_send.call_args[0][1]
    finally:
        ava._boot._agent_id = original


# ── priority ─────────────────────────────────────────────────────────────


def test_create_defaults_priority_p2(db_conn: psycopg.Connection) -> None:
    agent_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        task = task_registry.create("title", "detail")
        assert task.priority == "P2"
        assert _persisted_priority(db_conn, task.id) == "P2"
    finally:
        ava._boot._agent_id = original


def test_create_honours_explicit_priority(db_conn: psycopg.Connection) -> None:
    agent_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        task = task_registry.create("title", "detail", priority="P0")
        assert task.priority == "P0"
        assert _persisted_priority(db_conn, task.id) == "P0"
    finally:
        ava._boot._agent_id = original


def test_create_rejects_bad_priority(db_conn: psycopg.Connection) -> None:
    agent_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        with pytest.raises(ValueError, match="priority must be one of"):
            task_registry.create("title", "detail", priority="P9")
    finally:
        ava._boot._agent_id = original


def test_update_changes_priority(db_conn: psycopg.Connection) -> None:
    agent_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        task = task_registry.create("title", "detail")
        task_registry.update(task.id, priority="P1")
        assert _persisted_priority(db_conn, task.id) == "P1"
    finally:
        ava._boot._agent_id = original


def test_update_none_priority_is_noop(db_conn: psycopg.Connection) -> None:
    """priority=None means 'no change' — the task keeps its existing rung."""
    agent_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        task = task_registry.create("title", "detail", priority="P0")
        task_registry.update(task.id, status="in_progress", priority=None)
        assert _persisted_priority(db_conn, task.id) == "P0"
    finally:
        ava._boot._agent_id = original


def test_update_rejects_bad_priority(db_conn: psycopg.Connection) -> None:
    agent_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        task = task_registry.create("title", "detail")
        with pytest.raises(ValueError, match="priority must be one of"):
            task_registry.update(task.id, priority="nope")
    finally:
        ava._boot._agent_id = original


# ── SSE task events ────────────────────────────────────────────────────────


def test_create_publishes_task_created(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent_id = _seed_agent(db_conn)
    calls: list[tuple] = []
    monkeypatch.setattr(task_registry, "publish_task_created_sync", lambda *a: calls.append(a))  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        task = task_registry.create("title", "detail")
    finally:
        ava._boot._agent_id = original
    assert calls == [(agent_id, task.id)]


def test_update_publishes_task_updated(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        task = task_registry.create("title", "detail")
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
    assert params["parent"].default is None
    assert params["remind_interval_seconds"].default == 1800
    assert params["priority"].default == "P2"


def test_create_and_assign_returns_task_and_agent_id(db_conn: psycopg.Connection) -> None:
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
            task, aid = task_registry.create_and_assign("title", "description")  # pyright: ignore[reportUnknownMemberType]
        assert isinstance(task, task_registry.Task)
        assert task.title == "title"
        assert task.description == "description"
        assert aid == spawned_id
        mock_spawn.assert_called_once()
    finally:
        ava._boot._agent_id = original


def test_create_and_assign_task_owned_by_spawned_agent(db_conn: psycopg.Connection) -> None:
    """After create_and_assign, the task's owner is the spawned agent id."""
    agent_id = _seed_agent(db_conn)
    spawned_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        with patch("ava.agents.spawn", return_value=spawned_id), patch("ava.agents.send_message"):
            task, _ = task_registry.create_and_assign("title", "description")  # pyright: ignore[reportUnknownMemberType]
        assert task.owner == spawned_id
        assert _persisted_owner(db_conn, task.id) == spawned_id
    finally:
        ava._boot._agent_id = original


def test_create_and_assign_passes_spawn_args(db_conn: psycopg.Connection) -> None:
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
            )
        mock_spawn.assert_called_once_with(
            preset="researcher",
            label="test-label",
            config_overlay={"llm_model": "fast"},
            machine="test-machine",
        )
    finally:
        ava._boot._agent_id = original


def test_create_and_assign_sends_notification(db_conn: psycopg.Connection) -> None:
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
            task, _ = task_registry.create_and_assign("my title", "my description")  # pyright: ignore[reportUnknownMemberType]
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


def test_create_and_assign_no_notification_when_spawn_fails(db_conn: psycopg.Connection) -> None:
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
            task_registry.create_and_assign("title", "description")  # pyright: ignore[reportUnknownMemberType]
        mock_send.assert_not_called()
    finally:
        ava._boot._agent_id = original


def test_create_and_assign_honours_parent(db_conn: psycopg.Connection) -> None:
    """create_and_assign passes parent through to create."""
    agent_id = _seed_agent(db_conn)
    spawned_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        parent_task = task_registry.create("parent", "detail")
        with patch("ava.agents.spawn", return_value=spawned_id), patch("ava.agents.send_message"):
            task, _ = task_registry.create_and_assign("child", "detail", parent=parent_task.id)  # pyright: ignore[reportUnknownMemberType]
        assert task.parent_id == parent_task.id
    finally:
        ava._boot._agent_id = original


def test_create_and_assign_honours_remind_interval_seconds(db_conn: psycopg.Connection) -> None:
    """create_and_assign passes remind_interval_seconds through to create."""
    agent_id = _seed_agent(db_conn)
    spawned_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        with patch("ava.agents.spawn", return_value=spawned_id), patch("ava.agents.send_message"):
            task, _ = task_registry.create_and_assign(  # pyright: ignore[reportUnknownMemberType]
                "title", "detail", remind_interval_seconds=3600
            )
        assert task.remind_interval_seconds == 3600
    finally:
        ava._boot._agent_id = original


def test_create_and_assign_honours_priority(db_conn: psycopg.Connection) -> None:
    """create_and_assign passes priority through to create."""
    agent_id = _seed_agent(db_conn)
    spawned_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        with patch("ava.agents.spawn", return_value=spawned_id), patch("ava.agents.send_message"):
            task, _ = task_registry.create_and_assign("title", "detail", priority="P0")  # pyright: ignore[reportUnknownMemberType]
        assert task.priority == "P0"
        assert _persisted_priority(db_conn, task.id) == "P0"
    finally:
        ava._boot._agent_id = original


def test_create_and_assign_remind_interval_none(db_conn: psycopg.Connection) -> None:
    """create_and_assign with remind_interval_seconds=None falls back to the default —
    reminders cannot be disabled."""
    agent_id = _seed_agent(db_conn)
    spawned_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        with patch("ava.agents.spawn", return_value=spawned_id), patch("ava.agents.send_message"):
            task, _ = task_registry.create_and_assign(  # pyright: ignore[reportUnknownMemberType]
                "title", "detail", remind_interval_seconds=None
            )
        assert task.remind_interval_seconds == 1800
    finally:
        ava._boot._agent_id = original


def test_create_and_assign_uses_default_preset(db_conn: psycopg.Connection) -> None:
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
            task_registry.create_and_assign("title", "description")  # pyright: ignore[reportUnknownMemberType]
        mock_spawn.assert_called_once_with(
            preset="coder", label=None, config_overlay=None, machine=None
        )
    finally:
        ava._boot._agent_id = original


# ── create() — duplicate title rejection ──────────────────────────────────


def test_create_duplicate_open_title_raises(db_conn):
    """Creating a task with the same title as an open task raises ValueError."""
    agent_id = _seed_agent(db_conn)  # pyright: ignore[reportUnknownArgumentType]
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        task_registry.create("unique title", "detail")
        with pytest.raises(ValueError, match="already exists"):
            task_registry.create("unique title", "detail")
    finally:
        ava._boot._agent_id = original


def test_create_duplicate_in_progress_title_raises(db_conn):
    """Creating a task with the same title as an in_progress task raises ValueError."""
    agent_id = _seed_agent(db_conn)  # pyright: ignore[reportUnknownArgumentType]
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        task = task_registry.create("unique title 2", "detail")
        task_registry.update(task.id, status="in_progress")
        with pytest.raises(ValueError, match="already exists"):
            task_registry.create("unique title 2", "detail")
    finally:
        ava._boot._agent_id = original


def test_create_same_title_done_allowed(db_conn):
    """Creating a task with the same title as a done task is allowed."""
    agent_id = _seed_agent(db_conn)  # pyright: ignore[reportUnknownArgumentType]
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        task = task_registry.create("unique title 3", "detail")
        task_registry.update(task.id, status="done")
        task2 = task_registry.create("unique title 3", "detail")
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


def test_create_unique_violation_race_becomes_value_error(db_conn):
    """When two creates race past the app-level pre-check, the partial unique
    index rejects the second INSERT with UniqueViolation; create() translates
    that into the same ValueError agents see on the normal path."""
    agent_id = _seed_agent(db_conn)  # pyright: ignore[reportUnknownArgumentType]
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        task_registry.create("raced-title", "d")
        real_execute = psycopg.Cursor.execute
        with (
            patch.object(psycopg.Cursor, "execute", _fake_no_duplicate_precheck(real_execute)),  # pyright: ignore[reportUnknownArgumentType]
            pytest.raises(ValueError, match="already exists"),
        ):
            task_registry.create("raced-title", "d")
    finally:
        ava._boot._agent_id = original


def test_update_rename_race_becomes_value_error(db_conn):
    """Same backstop on the rename path: a concurrent rename that lands
    between update()'s pre-check and its UPDATE surfaces as ValueError, not a
    raw psycopg error."""
    agent_id = _seed_agent(db_conn)  # pyright: ignore[reportUnknownArgumentType]
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        task_registry.create("taken-title", "d")
        task = task_registry.create("free-title", "d")
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


def test_update_title_renames(db_conn):
    agent_id = _seed_agent(db_conn)  # pyright: ignore[reportUnknownArgumentType]
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        task = task_registry.create("old title", "detail")
        task_registry.update(task.id, title="new title")
        assert task_registry.get(task.id).title == "new title"
    finally:
        ava._boot._agent_id = original


def test_update_title_duplicate_open_raises(db_conn):
    """Renaming to another open/in_progress task's title raises ValueError —
    the same invariant create() enforces."""
    agent_id = _seed_agent(db_conn)  # pyright: ignore[reportUnknownArgumentType]
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        task_registry.create("taken title", "detail")
        task = task_registry.create("free title", "detail")
        with pytest.raises(ValueError, match="already exists"):
            task_registry.update(task.id, title="taken title")
        assert task_registry.get(task.id).title == "free title"
    finally:
        ava._boot._agent_id = original


def test_update_title_reassign_notifies_with_new_title(db_conn):
    """A rename and a reassignment in one update() notify with the new title."""
    agent_id = _seed_agent(db_conn)  # pyright: ignore[reportUnknownArgumentType]
    other_id = _seed_agent(db_conn)  # pyright: ignore[reportUnknownArgumentType]
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        task = task_registry.create("old title", "detail")
        with patch("ava.agents.send_message") as mock_send:
            task_registry.update(task.id, title="new title", owner=other_id)
        assert any("new title" in call.args[1] for call in mock_send.call_args_list)
    finally:
        ava._boot._agent_id = original


def test_create_same_title_cancelled_allowed(db_conn):
    """Creating a task with the same title as a cancelled task is allowed."""
    agent_id = _seed_agent(db_conn)  # pyright: ignore[reportUnknownArgumentType]
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        task = task_registry.create("unique title 4", "detail")
        task_registry.update(task.id, status="cancelled")
        task2 = task_registry.create("unique title 4", "detail")
        assert task2.id != task.id
    finally:
        ava._boot._agent_id = original


def test_create_unique_title_no_conflict(db_conn):
    """Creating a task with a truly unique title succeeds."""
    agent_id = _seed_agent(db_conn)  # pyright: ignore[reportUnknownArgumentType]
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        task1 = task_registry.create("title a", "detail")
        task2 = task_registry.create("title b", "detail")
        assert task1.id != task2.id
    finally:
        ava._boot._agent_id = original


# ── update() — note parameter ────────────────────────────────────────────


def test_update_note_with_cancel_appends_to_results(db_conn: psycopg.Connection) -> None:
    """note with status='cancelled' appends a timestamped line to results."""
    agent_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        task = task_registry.create("title", "detail")
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


def test_update_note_with_done_appends_to_results(db_conn: psycopg.Connection) -> None:
    """note works with any status, not just cancelled."""
    agent_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        task = task_registry.create("title", "detail")
        task_registry.update(task.id, status="done", note="all tests pass")
        results = task_registry.get(task.id).results
        assert results is not None
        assert "all tests pass" in results
    finally:
        ava._boot._agent_id = original


def test_update_note_standalone_no_status_change(db_conn: psycopg.Connection) -> None:
    """note alone (no status change) works as a drop-in for log()."""
    agent_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        task = task_registry.create("title", "detail")
        task_registry.update(task.id, note="progress update")
        results = task_registry.get(task.id).results
        assert results is not None
        assert "progress update" in results
        # Status unchanged
        assert task_registry.get(task.id).status == "open"
    finally:
        ava._boot._agent_id = original


def test_update_note_with_no_prior_results(db_conn: psycopg.Connection) -> None:
    """When a task has no prior results, the note line is the only content."""
    agent_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        task = task_registry.create("title", "detail")
        assert task.results is None
        task_registry.update(task.id, status="cancelled", note="duplicate")
        results = task_registry.get(task.id).results
        assert results is not None
        assert "duplicate" in results
    finally:
        ava._boot._agent_id = original


def test_update_note_with_results_overwrite_appends_after(
    db_conn: psycopg.Connection,
) -> None:
    """When results is also set, the note is appended after the new results."""
    agent_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        task = task_registry.create("title", "detail")
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


def test_update_note_none_is_noop(db_conn: psycopg.Connection) -> None:
    """note=None does nothing — no line appended."""
    agent_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        task = task_registry.create("title", "detail")
        task_registry.update(task.id, status="cancelled", note=None)
        results = task_registry.get(task.id).results
        # None because no note was appended and no prior results existed
        assert results is None
    finally:
        ava._boot._agent_id = original


def test_log_delegates_to_update_note(db_conn: psycopg.Connection) -> None:
    """log() is a thin wrapper around update(note=...)."""
    agent_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        task = task_registry.create("title", "detail")
        task_registry.log(task.id, "via log()")
        results = task_registry.get(task.id).results
        assert results is not None
        assert "via log()" in results
        # Status unchanged
        assert task_registry.get(task.id).status == "open"
    finally:
        ava._boot._agent_id = original


def _seed_root_task(db: psycopg.Connection) -> int:
    """Insert the system root task (is_root=TRUE, unowned) and return its id."""
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_tasks (title, description, status, created_by, is_root) "
            "VALUES ('Root', 'root', 'in_progress', 'system', TRUE) RETURNING id"
        )
        rid = cur.fetchone()[0]  # type: ignore[index]
    db.commit()
    return rid


def test_update_root_task_is_rejected(db_conn: psycopg.Connection) -> None:
    """The system root task is immutable: any update() targeting it fails fast
    and leaves the row untouched."""
    agent_id = _seed_agent(db_conn)
    root_id = _seed_root_task(db_conn)
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


# ── root task as default parent ──────────────────────────────────────────────


def _parent_of(db: psycopg.Connection, task_id: int) -> int | None:
    with db.cursor() as cur:
        cur.execute("SELECT parent_id FROM agent_tasks WHERE id = %s", (task_id,))
        row = cur.fetchone()
    assert row is not None
    return row[0]


def test_create_without_parent_anchors_to_root(db_conn: psycopg.Connection) -> None:
    """A task created without an explicit parent descends from the system root
    task — the root is resolved as its default parent. The root itself is never
    made its own parent (it stays parent-less)."""
    agent_id = _seed_agent(db_conn)
    root_id = _seed_root_task(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        task = task_registry.create("anchored-to-root", "detail")
        assert task.parent_id == root_id
        # The persisted row agrees, and the root did not gain a parent.
        assert _parent_of(db_conn, task.id) == root_id
        assert _parent_of(db_conn, root_id) is None
    finally:
        ava._boot._agent_id = original


def test_create_with_explicit_parent_overrides_root_default(db_conn: psycopg.Connection) -> None:
    """An explicit `parent` is used verbatim; root-anchoring only fills the
    default (parent=None) case. The explicit parent's own parent is the root
    (it was created without one)."""
    agent_id = _seed_agent(db_conn)
    root_id = _seed_root_task(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        parent = task_registry.create("explicit-parent", "detail")
        child = task_registry.create("explicit-child", "detail", parent=parent.id)
        assert parent.parent_id == root_id  # no explicit parent → anchored to root
        assert child.parent_id == parent.id  # explicit parent wins over the default
    finally:
        ava._boot._agent_id = original


def test_create_without_root_falls_back_to_parentless(db_conn: psycopg.Connection) -> None:
    """The degenerate uninitialized-DB case: with no root seeded, a parentless
    create() stays parent-less rather than failing (prod always has the seeded
    root, so this branch is only reached in a bare test DB)."""
    agent_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        task = task_registry.create("no-root-parentless", "detail")
        assert task.parent_id is None
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


def test_update_reparents_under_new_parent(db_conn: psycopg.Connection) -> None:
    agent_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        parent = task_registry.create("parent-task", "d")
        child = task_registry.create("child-task", "d")
        # create() anchors under the system root; reparent under `parent`.
        task_registry.update(child.id, parent_id=parent.id)
        assert _persisted_parent(db_conn, child.id) == parent.id
    finally:
        ava._boot._agent_id = original


def test_update_parent_none_moves_to_root(db_conn: psycopg.Connection) -> None:
    agent_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        parent = task_registry.create("parent-task2", "d")
        child = task_registry.create("child-task2", "d")
        task_registry.update(child.id, parent_id=parent.id)
        assert _persisted_parent(db_conn, child.id) == parent.id
        # The test DB has no seeded system root; seed one so the None path has
        # an anchor to resolve to (prod DBs always have exactly one).
        with db_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO agent_tasks (title, description, status, owner, created_by, is_root) "
                "VALUES ('Root', 'root', 'in_progress', %s, 'system', TRUE) RETURNING id",
                (agent_id,),
            )
            row = cur.fetchone()
            assert row is not None
            root_id = row[0]
        db_conn.commit()
        # Explicit None = back under the system root (same anchor create uses).
        task_registry.update(child.id, parent_id=None)
        assert _persisted_parent(db_conn, child.id) == root_id
    finally:
        ava._boot._agent_id = original


def test_update_rejects_self_parent(db_conn: psycopg.Connection) -> None:
    agent_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        task = task_registry.create("self-parent", "d")
        with pytest.raises(ValueError, match="own parent"):
            task_registry.update(task.id, parent_id=task.id)
    finally:
        ava._boot._agent_id = original


def test_update_rejects_missing_parent(db_conn: psycopg.Connection) -> None:
    agent_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        task = task_registry.create("missing-parent", "d")
        with pytest.raises(ValueError, match="does not exist"):
            task_registry.update(task.id, parent_id=999_999)
    finally:
        ava._boot._agent_id = original


def test_update_rejects_cycle_under_own_descendant(db_conn: psycopg.Connection) -> None:
    agent_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        a = task_registry.create("cycle-a", "d")
        b = task_registry.create("cycle-b", "d")
        c = task_registry.create("cycle-c", "d")
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


def test_update_parent_alone_is_a_valid_update(db_conn: psycopg.Connection) -> None:
    """A pure reparent (no other field) must not hit the nothing-to-update guard."""
    agent_id = _seed_agent(db_conn)
    original = ava._boot._agent_id
    ava._boot._agent_id = agent_id
    try:
        parent = task_registry.create("lone-parent", "d")
        child = task_registry.create("lone-child", "d")
        task_registry.update(child.id, parent_id=parent.id)
        assert _persisted_parent(db_conn, child.id) == parent.id
    finally:
        ava._boot._agent_id = original
