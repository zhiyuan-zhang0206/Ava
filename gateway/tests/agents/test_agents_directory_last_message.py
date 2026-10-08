"""Agent directory listing and last-message reads: page cards, lineage and detail-field contract, caller filtering; split from gateway/tests/test_agents_endpoints.py (task #4922)."""

from __future__ import annotations

import json

import psycopg
import pytest
from fastapi.testclient import TestClient

from gateway.app import app

# Every field of an `/api/agents` directory card; detail-only fields stay off it.
_DIRECTORY_CARD_KEYS = frozenset(
    {
        "agent_id",
        "spawner",
        "fork_source_agent_id",
        "status",
        "pid",
        "spawned_at",
        "started_at",
        "last_active_at",
        "last_inbound_at",
        "label",
        "machine",
        "supports_vision",
        "liveness_state",
        "awaiting_response_count",
        "highest_notice_priority",
        "unread_notice_count",
        "heartbeat_paused_until",
        "open_impersonation_session_id",
        "open_impersonation_status",
        "observation",
        "availability",
    }
)


class TestList:
    def test_get_agents_returns_page_with_status_and_lineage(
        self, db_conn: psycopg.Connection
    ) -> None:
        with TestClient(app) as client:
            a_id = client.post("/api/agents", json={}).json()["id"]
            b_id = client.post("/api/agents", json={"spawner": f"agent:{a_id}"}).json()["id"]
            with db_conn.cursor() as cur:
                cur.execute("UPDATE agents_meta SET status = 'idling' WHERE id = %s", (b_id,))
            db_conn.commit()
            resp = client.get("/api/agents")
        assert resp.status_code == 200
        page = resp.json()
        assert page["next_cursor"] is None
        rows = page["agents"]
        assert len(rows) == 2
        assert set(rows[0]) == _DIRECTORY_CARD_KEYS
        assert rows[0]["open_impersonation_session_id"] is None
        assert rows[0]["observation"]["runtime_owner"] == "unknown"
        by_id = {r["agent_id"]: r for r in rows}
        assert by_id[a_id]["status"] == "idling"
        assert by_id[a_id]["spawner"] == "user"
        assert by_id[b_id]["status"] == "idling"
        assert by_id[b_id]["spawner"] == f"agent:{a_id}"
        # spawn without prompt → label stays NULL (BackgroundTask LLM generation not triggered)
        assert by_id[a_id]["label"] is None
        assert by_id[b_id]["label"] is None

    def test_get_agents_scopes_live_and_terminated_in_the_database_contract(
        self,
        db_conn: psycopg.Connection,
    ) -> None:
        """History is explicit; the default contains all nonterminated states."""
        with TestClient(app) as client:
            live_id = client.post("/api/agents", json={}).json()["id"]
            terminated_id = client.post("/api/agents", json={}).json()["id"]
            with db_conn.cursor() as cur:
                cur.execute(
                    "UPDATE agents_meta SET status = 'terminated' WHERE id = %s",
                    (terminated_id,),
                )
            db_conn.commit()

            all_rows = client.get("/api/agents", params={"scope": "all"}).json()["agents"]
            default_rows = client.get("/api/agents").json()["agents"]
            live_rows = client.get("/api/agents", params={"scope": "live"}).json()["agents"]
            terminated_rows = client.get("/api/agents", params={"scope": "terminated"}).json()[
                "agents"
            ]
        assert {row["agent_id"] for row in all_rows} == {live_id, terminated_id}
        # Each request assesses availability at its own time.
        for row in default_rows + live_rows:
            row["availability"].pop("observed_at")
        assert default_rows == live_rows
        assert [row["agent_id"] for row in live_rows] == [live_id]
        assert [row["agent_id"] for row in terminated_rows] == [terminated_id]

    def test_get_agents_rejects_unknown_scope(self) -> None:
        with TestClient(app) as client:
            response = client.get("/api/agents", params={"scope": "future"})
        assert response.status_code == 422

    def test_directory_cards_omit_detail_only_fields(self, db_conn: psycopg.Connection) -> None:
        """List consumers receive only the fields they actually render or parse."""
        with TestClient(app) as client:
            agent_id = client.post("/api/agents", json={}).json()["id"]
            response = client.get("/api/agents")
            detail = client.get(f"/api/agents/{agent_id}")

        assert response.status_code == 200
        row = response.json()["agents"][0]
        assert set(row) == _DIRECTORY_CARD_KEYS
        assert row["open_impersonation_session_id"] is None
        assert row["open_impersonation_status"] is None
        assert row["observation"]["runtime_owner"] == "unknown"
        assert row["observation"]["machine_probe_at"] is None
        assert row["observation"]["machine_probe_valid_until"] is None
        assert detail.status_code == 200
        assert "fork_source_checkpoint_id" in detail.json()
        assert "last_probe_at" in detail.json()

    def test_directory_page_search_and_cursor(self, db_conn: psycopg.Connection) -> None:
        with TestClient(app) as client:
            ids = [client.post("/api/agents", json={}).json()["id"] for _ in range(4)]
            for agent_id in ids:
                client.patch(f"/api/agents/{agent_id}", json={"label": "alpha worker"})
            client.patch(f"/api/agents/{ids[2]}", json={"label": "unrelated"})
            first = client.get("/api/agents", params={"query": "alpha", "limit": 2}).json()
            second = client.get(
                "/api/agents",
                params={
                    "query": "alpha",
                    "limit": 2,
                    "before_id": first["next_cursor"],
                },
            ).json()
        assert [row["agent_id"] for row in first["agents"]] == [ids[3], ids[1]]
        assert first["next_cursor"] == ids[1]
        assert [row["agent_id"] for row in second["agents"]] == [ids[0]]
        assert second["next_cursor"] is None

    @pytest.mark.parametrize(
        "params",
        [
            {"limit": 0},
            {"limit": 201},
            {"before_id": 0},
            {"query": "x" * 201},
        ],
    )
    def test_directory_rejects_invalid_page_bounds(self, params: dict[str, str | int]) -> None:
        with TestClient(app) as client:
            response = client.get("/api/agents", params=params)
        assert response.status_code == 422

    def test_directory_has_one_card_contract(self) -> None:
        parameters = app.openapi()["paths"]["/api/agents"]["get"]["parameters"]
        assert {parameter["name"] for parameter in parameters} == {
            "scope",
            "query",
            "before_id",
            "limit",
        }

    def test_get_agents_joins_thread_label(self, db_conn: psycopg.Connection) -> None:
        """label field fetched from agents JOIN — after PATCH write, GET should see it."""
        with TestClient(app) as client:
            a_id = client.post("/api/agents", json={}).json()["id"]
            client.patch(f"/api/agents/{a_id}", json={"label": "\u6211\u7684 agent"})
            resp = client.get("/api/agents")
        assert resp.status_code == 200
        by_id = {r["agent_id"]: r for r in resp.json()["agents"]}
        assert by_id[a_id]["label"] == "\u6211\u7684 agent"

    def test_get_single_agent_returns_label(self, db_conn: psycopg.Connection) -> None:
        with TestClient(app) as client:
            a_id = client.post("/api/agents", json={}).json()["id"]
            client.patch(f"/api/agents/{a_id}", json={"label": "single-x"})
            resp = client.get(f"/api/agents/{a_id}")
        assert resp.status_code == 200
        assert resp.json()["label"] == "single-x"

    def test_get_agents_returns_machine_column(self, db_conn: psycopg.Connection) -> None:
        """AgentRow.machine comes from agents_meta.machine — frontend sidebar uses it to display
        machine badge + fork picker default placement."""
        with TestClient(app) as client:
            a_id = client.post("/api/agents", json={}).json()["id"]
            # manually change machine to simulate cross-machine deployment (default spawn_agent writes local machine_name())
            with db_conn.cursor() as cur:
                cur.execute("UPDATE agents_meta SET machine = 'test-host' WHERE id = %s", (a_id,))
            db_conn.commit()
            list_resp = client.get("/api/agents")
            single_resp = client.get(f"/api/agents/{a_id}")
        assert list_resp.status_code == 200
        assert single_resp.status_code == 200
        list_row = next(r for r in list_resp.json()["agents"] if r["agent_id"] == a_id)
        assert list_row["machine"] == "test-host"
        assert single_resp.json()["machine"] == "test-host"

    def test_get_agents_empty_returns_page_without_cursor(
        self,
        db_conn: psycopg.Connection,
    ) -> None:
        with TestClient(app) as client:
            resp = client.get("/api/agents")
        assert resp.status_code == 200
        assert resp.json() == {"agents": [], "next_cursor": None}


class TestGetLastMessage:
    def test_any_agent_can_query_unrelated_agent(self, db_conn: psycopg.Connection) -> None:
        """Any agent in the cluster can query — not just spawn-chain ancestors."""
        from base.db import create_agent

        # Create two unrelated agents (no spawn chain).
        # create_agent inserts into agents (LangGraph thread); agents_meta
        # carries the row the endpoint reads.
        agent_a = create_agent(db_conn)
        agent_b = create_agent(db_conn)
        with db_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO agents_meta (id, spawner, status, last_message_text) "
                "VALUES (%s, 'test', 'running', %s)",
                (agent_a, "hello from agent A"),
            )
            cur.execute(
                "INSERT INTO agents_meta (id, spawner, status) VALUES (%s, 'test', 'running')",
                (agent_b,),
            )
        db_conn.commit()

        with TestClient(app) as client:
            # Agent B queries Agent A's last message — should succeed
            resp = client.get(
                f"/api/agents/{agent_a}/last-message",
                params={"caller": f"agent:{agent_b}"},
            )
        assert resp.status_code == 200
        assert resp.json()["text"] == "hello from agent A"

    @pytest.mark.parametrize("isolation_column", ["config_overlay", "birth_config"])
    def test_eval_isolated_caller_is_denied(
        self, db_conn: psycopg.Connection, isolation_column: str
    ) -> None:
        """The gateway denies the result read even if an eval agent bypasses its SDK."""
        from base.db import create_agent

        target_id = create_agent(db_conn)
        caller_id = create_agent(db_conn)
        with db_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO agents_meta (id, spawner, status, last_message_text) "
                "VALUES (%s, 'test', 'running', %s)",
                (target_id, "source result"),
            )
            if isolation_column == "config_overlay":
                cur.execute(
                    "INSERT INTO agents_meta (id, spawner, status, config_overlay) "
                    "VALUES (%s, 'test', 'running', %s::jsonb)",
                    (caller_id, json.dumps({"eval_isolation": True})),
                )
            else:
                cur.execute(
                    "INSERT INTO agents_meta (id, spawner, status, birth_config) "
                    "VALUES (%s, 'test', 'running', %s::jsonb)",
                    (caller_id, json.dumps({"eval_isolation": True})),
                )
        db_conn.commit()

        with TestClient(app) as client:
            resp = client.get(
                f"/api/agents/{target_id}/last-message",
                params={"caller": f"agent:{caller_id}"},
            )

        assert resp.status_code == 403
        assert "eval-isolated" in resp.json()["detail"]

    def test_none_for_agent_without_ai_message(self, db_conn: psycopg.Connection) -> None:
        """Returns text=None when the agent has no AI message yet."""
        from base.db import create_agent

        agent_id = create_agent(db_conn)
        with db_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO agents_meta (id, spawner, status) VALUES (%s, 'test', 'running')",
                (agent_id,),
            )
        db_conn.commit()

        with TestClient(app) as client:
            resp = client.get(
                f"/api/agents/{agent_id}/last-message",
                params={"caller": "agent:99999"},
            )
        assert resp.status_code == 200
        assert resp.json()["text"] is None

    def test_returns_last_message_text_from_column(self, db_conn: psycopg.Connection) -> None:
        """When last_message_text is set, return it — no checkpoint needed."""
        from base.db import create_agent

        agent_id = create_agent(db_conn)
        with db_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO agents_meta (id, spawner, status, last_message_text) "
                "VALUES (%s, 'test', 'running', %s)",
                (agent_id, "hello from column"),
            )
        db_conn.commit()

        with TestClient(app) as client:
            resp = client.get(
                f"/api/agents/{agent_id}/last-message",
                params={"caller": "agent:99999"},
            )
        assert resp.status_code == 200
        assert resp.json()["text"] == "hello from column"

    def test_last_message_text_survives_without_checkpoint(
        self, db_conn: psycopg.Connection
    ) -> None:
        """After compact wipes the checkpoint, last_message_text still returns the last AI text."""
        from base.db import create_agent

        agent_id = create_agent(db_conn)
        with db_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO agents_meta (id, spawner, status, last_message_text) "
                "VALUES (%s, 'test', 'running', %s)",
                (agent_id, "pre-compact message"),
            )
        db_conn.commit()

        # No checkpoint written — simulating post-compact state where
        # checkpoint only has [SystemMessage, summary] (no AIMessage).
        with TestClient(app) as client:
            resp = client.get(
                f"/api/agents/{agent_id}/last-message",
                params={"caller": "agent:99999"},
            )
        assert resp.status_code == 200
        assert resp.json()["text"] == "pre-compact message"

    def test_empty_text_reads_as_none(self, db_conn: psycopg.Connection) -> None:
        """An empty-string column value reads as None — no empty message."""
        from base.db import create_agent

        agent_id = create_agent(db_conn)
        with db_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO agents_meta (id, spawner, status, last_message_text) "
                "VALUES (%s, 'test', 'running', '')",
                (agent_id,),
            )
        db_conn.commit()

        with TestClient(app) as client:
            resp = client.get(
                f"/api/agents/{agent_id}/last-message",
                params={"caller": "agent:99999"},
            )
        assert resp.status_code == 200
        assert resp.json()["text"] is None

    def test_404_for_nonexistent_agent(self, db_conn: psycopg.Connection) -> None:
        with TestClient(app) as client:
            resp = client.get(
                "/api/agents/99999/last-message",
                params={"caller": "agent:1"},
            )
        assert resp.status_code == 404
