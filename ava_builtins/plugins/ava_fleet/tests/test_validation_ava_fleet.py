"""Argument validation of the fleet plugin's task and notice entry points."""

from __future__ import annotations

from typing import Any

import psycopg
import pytest

from tests.fixtures.pin_agent import pin_agent


class TestTasksEntries:
    def test_create_title_unwraps(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Single-element title tuple lands as the unwrapped string (DB round-trip)."""
        from ava_builtins.plugins.ava_fleet import task_registry

        with db_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO agent_tasks (title, description, status, created_by, is_root) "
                "VALUES ('Root', 'root', 'in_progress', 'system', TRUE) RETURNING id"
            )
            row = cur.fetchone()
            assert row is not None
            root_id = row[0]  # pyright: ignore[reportOptionalSubscript]
        db_conn.commit()
        pin_agent(900001)
        with db_conn.cursor() as cur:
            cur.execute("INSERT INTO agents (id) VALUES (900001) ON CONFLICT (id) DO NOTHING")
        db_conn.commit()
        from base.events.live import announce

        monkeypatch.setattr(announce, "publish_task_created_sync", lambda *_a, **_k: None)  # pyright: ignore[reportUnknownArgumentType]

        task = task_registry.create(title=("My Task",), description="d", parent=root_id)  # pyright: ignore[reportArgumentType, reportUnknownArgumentType]
        assert task.title == "My Task"

    def test_create_multi_element_title_type_errors(self) -> None:
        from ava_builtins.plugins.ava_fleet import task_registry

        with pytest.raises(TypeError, match="title must be a string"):
            task_registry.create(title=("a", "b"), description="d", parent=1)  # pyright: ignore[reportArgumentType]

    def test_create_parent_never_unwraps(self) -> None:
        from ava_builtins.plugins.ava_fleet import task_registry

        with pytest.raises(TypeError, match="parent must be int"):
            task_registry.create(title="t", description="d", parent=(1,))  # pyright: ignore[reportArgumentType]

    def test_log_message_unwraps(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from ava_builtins.plugins.ava_fleet import task_registry

        seen: dict[str, Any] = {}
        monkeypatch.setattr(task_registry, "update", lambda *_a, **_kw: seen.update(_kw))  # pyright: ignore[reportUnknownArgumentType]
        task_registry.log(7, ("note",))  # pyright: ignore[reportArgumentType]
        assert seen["note"] == "note"

    def test_log_multi_element_type_errors(self) -> None:
        from ava_builtins.plugins.ava_fleet import task_registry

        with pytest.raises(TypeError, match="message must be a string"):
            task_registry.log(7, ("a", "b"))  # pyright: ignore[reportArgumentType]

    def test_update_status_unwraps(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from ava_builtins.plugins.ava_fleet import task_registry

        # Coercion fires before the DB write: a status tuple unwraps, then the
        # status-value validation runs on the string.
        with pytest.raises(ValueError, match="status must be one of"):
            task_registry.update(7, status=("not-a-status",))  # pyright: ignore[reportArgumentType]

    def test_update_owner_never_unwraps(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from ava_builtins.plugins.ava_fleet import task_registry

        with pytest.raises(TypeError, match="owner must be int"):
            task_registry.update(7, owner=(5,))  # pyright: ignore[reportArgumentType]


class TestNoticeEntries:
    def test_notify_title_unwraps(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from ava import gateway_client
        from ava_builtins.plugins.ava_fleet import plugin as fleet_plugin

        seen: dict[str, Any] = {}
        monkeypatch.setattr(
            gateway_client,
            "post",
            lambda *_a, **_kw: seen.update(body=_a[1]) or _FakeResp(),  # pyright: ignore[reportUnknownArgumentType]
        )  # pyright: ignore[reportUnknownArgumentType]
        monkeypatch.setattr(gateway_client, "raise_from_response", lambda _resp: None)  # pyright: ignore[reportUnknownArgumentType]
        pin_agent(900001)

        notice = fleet_plugin.notify(("Hi",), ("detail",), priority=("P2",))  # pyright: ignore[reportArgumentType]
        body = seen["body"]
        assert body["title"] == "Hi"
        assert body["content"] == "detail"
        assert body["priority"] == "P2"
        assert notice == 1  # Notice is an int subclass; the id is the value

    def test_notify_multi_element_title_type_errors(self) -> None:
        from ava_builtins.plugins.ava_fleet import plugin as fleet_plugin

        with pytest.raises(TypeError, match="title must be a string"):
            fleet_plugin.notify(("a", "b"))  # pyright: ignore[reportArgumentType]

    def test_notify_task_never_unwraps(self) -> None:
        from ava_builtins.plugins.ava_fleet import plugin as fleet_plugin

        with pytest.raises(TypeError, match="task must be int"):
            fleet_plugin.notify("hi", task=("5",))  # pyright: ignore[reportArgumentType]


class _FakeResp:
    """Minimal stand-in for the gateway response notify() consumes."""

    def json(self) -> dict[str, Any]:
        return {
            "id": 1,
            "pending_count": 0,
            "superseded": [],
            "pending_notices": [],
        }
