"""System-note and restart endpoints: inbound insertion, task-note tagging, config-overlay merge, delivery and resurrection behavior; split from gateway/tests/test_agents_endpoints.py (task #4922)."""

from __future__ import annotations

import json
import threading
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg_pool import ConnectionPool

from base.db import Database
from base.events.live.bus import EventBus
from gateway.app import app
from gateway.tests.test_agents_endpoints import _inbound_rows, _returned_id, _terminate_hosted
from gateway.tests.test_agents_endpoints import withdrawn_model as withdrawn_model


class TestSystemNote:
    def test_system_note_inserts_system_note_inbound(self, db_conn: psycopg.Connection) -> None:
        """POST /system-note → kind='system_note' inbound with the task note tag
        (no peer-chat row), delivered to a live agent without resurrection."""
        with TestClient(app) as client:
            agent_id = client.post("/api/agents", json={}).json()["id"]
            resp = client.post(
                f"/api/agents/{agent_id}/system-note",
                json={"content": 'Task #1 "t" is now assigned to you.'},
                headers={"Idempotency-Key": str(uuid4())},
            )
        assert resp.status_code == 201
        body = resp.json()
        assert body["status"] == "idling"
        assert body["inbound_id"] is not None
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT content, kind, source, payload FROM inbound_messages WHERE agent_id = %s",
                (agent_id,),
            )
            rows = cur.fetchall()
        assert len(rows) == 1
        content, kind, source, payload = rows[0]
        assert kind == "system_note"
        assert source == "system"
        assert "assigned to you" in content
        assert payload == {"note_tag": "task", "delivery_resurrect": True}
        # No resurrect row for a live agent.
        assert all(r[1] != "resurrect" for r in rows)

    def test_system_note_to_terminated_agent_resurrects_when_requested(
        self, db_conn: psycopg.Connection
    ) -> None:
        """resurrect=True (task assignment) revives a terminated target, like chat."""
        with TestClient(app) as client:
            agent_id = client.post("/api/agents", json={}).json()["id"]
            _terminate_hosted(db_conn, agent_id)
            resp = client.post(
                f"/api/agents/{agent_id}/system-note",
                json={"content": 'Task #1 "t" is now assigned to you.'},
                headers={"Idempotency-Key": str(uuid4())},
            )
        assert resp.status_code == 201
        rows = _inbound_rows(db_conn, agent_id)
        # Auto-resurrect inserts its 'resurrect' lifecycle inbound before the note
        assert ("", "resurrect", "system") in rows
        assert any(kind == "system_note" for _, kind, _ in rows)

    def test_system_note_to_terminated_agent_no_resurrect_when_denied(
        self, db_conn: psycopg.Connection
    ) -> None:
        """resurrect=False (plain update / reminder notice) never revives a
        terminated owner — the note stays queued (user ruling 2026-08-27)."""
        with TestClient(app) as client:
            agent_id = client.post("/api/agents", json={}).json()["id"]
            _terminate_hosted(db_conn, agent_id)
            resp = client.post(
                f"/api/agents/{agent_id}/system-note",
                json={"content": 'Task #1 "t" was updated.', "resurrect": False},
                headers={"Idempotency-Key": str(uuid4())},
            )
        assert resp.status_code == 201
        rows = _inbound_rows(db_conn, agent_id)
        assert rows == [('Task #1 "t" was updated.', "system_note", "system")]
        assert all(r[1] != "resurrect" for r in rows)

    def test_system_note_unknown_tag_rejected_422(self, db_conn: psycopg.Connection) -> None:
        """note_tag outside the closed NoteTag set → 422 (fail loud)."""
        with TestClient(app) as client:
            agent_id = client.post("/api/agents", json={}).json()["id"]
            resp = client.post(
                f"/api/agents/{agent_id}/system-note",
                json={"content": "x", "note_tag": "not_a_tag"},
                headers={"Idempotency-Key": str(uuid4())},
            )
        assert resp.status_code == 422

    def test_system_note_task_id_requires_task_tag(self, db_conn: psycopg.Connection) -> None:
        """Task attribution cannot silently ride an unrelated system-note kind."""
        with TestClient(app) as client:
            agent_id = client.post("/api/agents", json={}).json()["id"]
            resp = client.post(
                f"/api/agents/{agent_id}/system-note",
                json={"content": "x", "note_tag": "heartbeat", "task_id": 42},
                headers={"Idempotency-Key": str(uuid4())},
            )
        assert resp.status_code == 422
        assert "task_id requires note_tag='task'" in str(resp.json())

    def test_system_note_task_id_must_name_an_existing_task(
        self, db_conn: psycopg.Connection
    ) -> None:
        """A task note must link to an existing task."""
        with TestClient(app) as client:
            agent_id = client.post("/api/agents", json={}).json()["id"]
            resp = client.post(
                f"/api/agents/{agent_id}/system-note",
                json={"content": "x", "task_id": 999_999},
                headers={"Idempotency-Key": str(uuid4())},
            )
        assert resp.status_code == 422
        assert "task_id 999999 does not exist" in str(resp.json())

    def test_system_note_task_id_must_belong_to_the_recipient(
        self, db_conn: psycopg.Connection
    ) -> None:
        """A task note cannot charge one agent's task for another agent's turn."""
        with TestClient(app) as client:
            owner_id = client.post("/api/agents", json={}).json()["id"]
            recipient_id = client.post("/api/agents", json={}).json()["id"]
            with db_conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO agent_tasks (title, description, created_by, owner) "
                    "VALUES ('owned task', 'd', 'user', %s) RETURNING id",
                    (owner_id,),
                )
                row = cur.fetchone()
            assert row is not None
            db_conn.commit()
            resp = client.post(
                f"/api/agents/{recipient_id}/system-note",
                json={"content": "x", "task_id": row[0]},
                headers={"Idempotency-Key": str(uuid4())},
            )
        assert resp.status_code == 422
        assert "is not owned by agent" in str(resp.json())

    def test_system_note_task_ownership_stays_locked_through_enqueue(
        self,
        db_conn: psycopg.Connection,
        monkeypatch: pytest.MonkeyPatch,
        database: Database,
        event_bus: EventBus,
    ) -> None:
        """A reassignment cannot land after validation but before task-note enqueueing."""
        from base.db import connect, pool
        from gateway.agents import system_note

        enqueue_entered, release_enqueue, reassign_started, reassign_finished = (
            threading.Event(),
            threading.Event(),
            threading.Event(),
            threading.Event(),
        )
        errors: list[Exception] = []

        writer = system_note.insert_inbound_message_in_transaction

        def pause_enqueue(*args: Any, **kwargs: Any):
            enqueue_entered.set()
            assert release_enqueue.wait(timeout=2)
            return writer(*args, **kwargs)

        with db_conn.cursor() as cur:
            cur.execute("INSERT INTO agents DEFAULT VALUES RETURNING id")
            owner_id = _returned_id(cur)
            cur.execute("INSERT INTO agents DEFAULT VALUES RETURNING id")
            new_owner_id = _returned_id(cur)
            cur.execute(
                "INSERT INTO agent_tasks (title, description, created_by, owner) "
                "VALUES ('locked task', 'd', 'user', %s) RETURNING id",
                (owner_id,),
            )
            task_id = _returned_id(cur)
        db_conn.commit()
        monkeypatch.setattr(system_note, "insert_inbound_message_in_transaction", pause_enqueue)

        def enqueue(note_pool: ConnectionPool) -> None:
            try:
                system_note._system_note_blocking(
                    database,
                    event_bus,
                    note_pool,
                    owner_id,
                    "x",
                    "system",
                    "task",
                    task_id,
                    client_message_id="task-owner-fence",
                )
            except Exception as exc:
                errors.append(exc)

        def reassign() -> None:
            try:
                with connect() as conn, conn.cursor() as cur:
                    reassign_started.set()
                    cur.execute(
                        "UPDATE agent_tasks SET owner = %s WHERE id = %s",
                        (new_owner_id, task_id),
                    )
            except Exception as exc:
                errors.append(exc)
            finally:
                reassign_finished.set()

        with pool(max_size=1) as note_pool:
            enqueue_thread, reassign_thread = (
                threading.Thread(target=enqueue, args=(note_pool,), daemon=True),
                threading.Thread(target=reassign, daemon=True),
            )
            enqueue_thread.start()
            try:
                assert enqueue_entered.wait(timeout=2), errors
                reassign_thread.start()
                assert reassign_started.wait(timeout=2)
                assert not reassign_finished.wait(timeout=0.2), (
                    "task reassignment committed while the task note was being enqueued"
                )
            finally:
                release_enqueue.set()
                enqueue_thread.join(timeout=2)
                if reassign_thread.ident is not None:
                    reassign_thread.join(timeout=2)

        assert not enqueue_thread.is_alive()
        assert not reassign_thread.is_alive()
        assert errors == []

    def test_system_note_illegal_source_rejected_422(self, db_conn: psycopg.Connection) -> None:
        """source not in envelope allowlist → 422."""
        with TestClient(app) as client:
            agent_id = client.post("/api/agents", json={}).json()["id"]
            resp = client.post(
                f"/api/agents/{agent_id}/system-note",
                json={"content": "x", "source": "ui:web"},
                headers={"Idempotency-Key": str(uuid4())},
            )
        assert resp.status_code == 422


class TestRestart:
    @pytest.mark.parametrize(
        ("initial", "overlay"),
        [
            (
                {"llm_model": "deepseek-flash", "reasoning_effort": "max"},
                {"reasoning_effort": "low"},
            ),
            (
                {"llm_model": "gpt-5.6-sol", "reasoning_effort": "low"},
                {"llm_model": "deepseek-flash"},
            ),
        ],
    )
    def test_incompatible_merged_model_effort_is_rejected_without_writes(
        self, db_conn: psycopg.Connection, initial: dict[str, str], overlay: dict[str, str]
    ) -> None:
        with TestClient(app) as client:
            spawned = client.post("/api/agents", json={"config": initial})
            assert spawned.status_code == 201, spawned.text
            agent_id = spawned.json()["id"]
            response = client.post(
                f"/api/agents/{agent_id}/restart", json={"config_overlay": overlay}
            )
        assert response.status_code == 400, response.text
        assert "unsupported reasoning effort" in response.json()["detail"]
        assert db_conn.execute(
            "SELECT config_overlay FROM agents_meta WHERE id=%s", (agent_id,)
        ).fetchone() == (initial,)
        assert db_conn.execute(
            "SELECT count(*) FROM inbound_messages WHERE agent_id=%s AND kind='restart'",
            (agent_id,),
        ).fetchone() == (0,)

    def test_restart_inserts_inbound_and_returns_enqueued(
        self, db_conn: psycopg.Connection
    ) -> None:
        with TestClient(app) as client:
            agent_id = client.post("/api/agents", json={}).json()["id"]
            resp = client.post(f"/api/agents/{agent_id}/restart")
        assert resp.status_code == 200
        assert resp.json() == {"status": "enqueued"}
        assert _inbound_rows(db_conn, agent_id) == [("", "restart", "user")]

    def test_restart_merges_config_overlay_and_records_it_in_the_inbound(
        self, db_conn: psycopg.Connection
    ) -> None:
        """The persisted overlay and restart marker payload advance together."""
        with TestClient(app) as client:
            agent_id = client.post("/api/agents", json={}).json()["id"]
            with db_conn.cursor() as cur:
                cur.execute(
                    "UPDATE agents_meta SET config_overlay = %s::jsonb WHERE id = %s",
                    (json.dumps({"reasoning_effort": "low"}), agent_id),
                )
            db_conn.commit()
            resp = client.post(
                f"/api/agents/{agent_id}/restart",
                json={"config_overlay": {"llm_model": "gpt-5.6-sol"}},
            )

        assert resp.status_code == 200
        with db_conn.cursor() as cur:
            cur.execute("SELECT config_overlay FROM agents_meta WHERE id = %s", (agent_id,))
            assert cur.fetchone() == ({"reasoning_effort": "low", "llm_model": "gpt-5.6-sol"},)
            cur.execute(
                "SELECT payload FROM inbound_messages WHERE agent_id = %s AND kind = 'restart'",
                (agent_id,),
            )
            assert cur.fetchone() == ({"config_overlay": {"llm_model": "gpt-5.6-sol"}},)

    def test_restart_settles_withdrawn_model_before_storing(
        self, db_conn: psycopg.Connection, withdrawn_model: str
    ) -> None:
        """The ops restart channel (the provider-outage model switch) settles a
        withdrawn llm_model to its registered fallback in both the persisted
        overlay and the restart payload (task #4306)."""
        with TestClient(app) as client:
            agent_id = client.post("/api/agents", json={}).json()["id"]
            resp = client.post(
                f"/api/agents/{agent_id}/restart",
                json={"config_overlay": {"llm_model": withdrawn_model}},
            )
        assert resp.status_code == 200
        with db_conn.cursor() as cur:
            cur.execute("SELECT config_overlay FROM agents_meta WHERE id = %s", (agent_id,))
            assert cur.fetchone() == ({"llm_model": "deepseek-flash"},)
            cur.execute(
                "SELECT payload FROM inbound_messages WHERE agent_id = %s AND kind = 'restart'",
                (agent_id,),
            )
            assert cur.fetchone() == ({"config_overlay": {"llm_model": "deepseek-flash"}},)

    @pytest.mark.parametrize("config_overlay", [None, {}])
    def test_restart_empty_config_overlay_keeps_legacy_restart_shape(
        self, db_conn: psycopg.Connection, config_overlay: dict[str, object] | None
    ) -> None:
        """None and {} do not change persistent config or add a payload sidecar."""
        with TestClient(app) as client:
            agent_id = client.post("/api/agents", json={}).json()["id"]
            with db_conn.cursor() as cur:
                cur.execute(
                    "UPDATE agents_meta SET config_overlay = %s::jsonb WHERE id = %s",
                    (json.dumps({"reasoning_effort": "low"}), agent_id),
                )
            db_conn.commit()
            resp = client.post(
                f"/api/agents/{agent_id}/restart",
                json={"config_overlay": config_overlay},
            )

        assert resp.status_code == 200
        with db_conn.cursor() as cur:
            cur.execute("SELECT config_overlay FROM agents_meta WHERE id = %s", (agent_id,))
            assert cur.fetchone() == ({"reasoning_effort": "low"},)
            cur.execute(
                "SELECT payload FROM inbound_messages WHERE agent_id = %s AND kind = 'restart'",
                (agent_id,),
            )
            assert cur.fetchone() == (None,)

    def test_restart_invalid_config_overlay_is_rejected_before_enqueue(
        self, db_conn: psycopg.Connection
    ) -> None:
        with TestClient(app) as client:
            agent_id = client.post("/api/agents", json={}).json()["id"]
            resp = client.post(
                f"/api/agents/{agent_id}/restart",
                json={"config_overlay": {"definitely_not_a_config_field": "x"}},
            )

        assert resp.status_code == 422
        assert _inbound_rows(db_conn, agent_id) == []

    def test_restart_already_terminated_is_noop(self, db_conn: psycopg.Connection) -> None:
        with TestClient(app) as client:
            agent_id = client.post("/api/agents", json={}).json()["id"]
            with db_conn.cursor() as cur:
                cur.execute(
                    "UPDATE agents_meta SET status = 'terminated' WHERE id = %s", (agent_id,)
                )
            db_conn.commit()
            resp = client.post(f"/api/agents/{agent_id}/restart")
        assert resp.status_code == 200
        assert resp.json() == {"status": "already_terminated"}
        assert _inbound_rows(db_conn, agent_id) == []  # not delivered

    def test_restart_nonexistent_404(self, db_conn: psycopg.Connection) -> None:
        with TestClient(app) as client:
            resp = client.post("/api/agents/9999/restart")
        assert resp.status_code == 404
