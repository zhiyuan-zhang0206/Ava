"""Argument validation of the fleet plugin's task and notice entry points."""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import httpx
import psycopg
import pytest

import meiva
from meiva.tests.fixtures.pin_agent import pin_agent


class TestTasksEntries:
    def test_create_title_unwraps(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Single-element title tuple lands as the unwrapped string (DB round-trip)."""
        from meiva_builtins.plugins.meiva_fleet import task_registry

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

        inputs: dict[str, Any] = {
            "title": ("My Task",),
            "description": "d",
            "parent": root_id,
            "operation_key": str(uuid4()),
        }
        task = task_registry.create(**inputs)
        assert task.title == "My Task"

    def test_create_multi_element_title_type_errors(self) -> None:
        from meiva_builtins.plugins.meiva_fleet import task_registry

        with pytest.raises(TypeError, match="title must be a string"):
            inputs: dict[str, Any] = {
                "title": ("a", "b"),
                "description": "d",
                "parent": 1,
                "operation_key": str(uuid4()),
            }
            task_registry.create(**inputs)

    def test_create_parent_never_unwraps(self) -> None:
        from meiva_builtins.plugins.meiva_fleet import task_registry

        with pytest.raises(TypeError, match="parent must be int"):
            inputs: dict[str, Any] = {
                "title": "t",
                "description": "d",
                "parent": (1,),
                "operation_key": str(uuid4()),
            }
            task_registry.create(**inputs)

    def test_log_message_unwraps(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from meiva_builtins.plugins.meiva_fleet import task_registry

        seen: dict[str, Any] = {}
        monkeypatch.setattr(task_registry, "update", lambda *_a, **_kw: seen.update(_kw))  # pyright: ignore[reportUnknownArgumentType]
        task_registry.log(7, ("note",), operation_key=str(uuid4()))  # pyright: ignore[reportArgumentType]
        assert seen["note"] == "note"

    def test_log_multi_element_type_errors(self) -> None:
        from meiva_builtins.plugins.meiva_fleet import task_registry

        with pytest.raises(TypeError, match="message must be a string"):
            task_registry.log(7, ("a", "b"), operation_key=str(uuid4()))  # pyright: ignore[reportArgumentType]

    def test_update_status_unwraps(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from meiva_builtins.plugins.meiva_fleet import task_registry

        # Coercion fires before the DB write: a status tuple unwraps, then the
        # status-value validation runs on the string.
        with pytest.raises(ValueError, match="status must be one of"):
            task_registry.update(7, status=("not-a-status",), operation_key=str(uuid4()))  # pyright: ignore[reportArgumentType]

    def test_update_owner_never_unwraps(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from meiva_builtins.plugins.meiva_fleet import task_registry

        with pytest.raises(TypeError, match="owner must be int"):
            task_registry.update(7, owner=(5,), operation_key=str(uuid4()))  # pyright: ignore[reportArgumentType]


class TestNoticeEntries:
    def test_notify_title_unwraps(self) -> None:
        from meiva_builtins.plugins.meiva_fleet import plugin as fleet_plugin

        requests: list[httpx.Request] = []

        def answer(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(
                201,
                json={
                    "id": 1,
                    "pending_count": 0,
                    "superseded": [],
                    "pending_notices": [],
                },
            )

        pin_agent(900001)
        inputs: dict[str, Any] = {
            "title": ("Hi",),
            "content": ("detail",),
            "priority": ("P2",),
            "idempotency_key": "notice-intent",
        }
        with (
            httpx.Client(base_url="http://gateway", transport=httpx.MockTransport(answer)) as http,
            meiva.context.clients.using_gateway(http),
        ):
            notice = fleet_plugin.notify(**inputs)
        [request] = requests
        assert (request.method, request.url.path) == ("POST", "/api/agents/900001/notices")
        body = json.loads(request.content)
        assert body["title"] == "Hi"
        assert body["content"] == "detail"
        assert body["priority"] == "P2"
        assert request.headers["Idempotency-Key"] == "notice-intent"
        assert notice == 1

    def test_notify_multi_element_title_type_errors(self) -> None:
        from meiva_builtins.plugins.meiva_fleet import plugin as fleet_plugin

        with pytest.raises(TypeError, match="title must be a string"):
            fleet_plugin.notify(("a", "b"), idempotency_key=str(uuid4()))  # pyright: ignore[reportArgumentType]

    def test_notify_task_never_unwraps(self) -> None:
        from meiva_builtins.plugins.meiva_fleet import plugin as fleet_plugin

        with pytest.raises(TypeError, match="task must be int"):
            fleet_plugin.notify("hi", task=("5",), idempotency_key=str(uuid4()))  # pyright: ignore[reportArgumentType]
