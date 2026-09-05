"""Tests for gateway/routers/presets.py (the /api/presets CRUD surface) and the
spawn-with-preset config merge in gateway/routers/agents.py.

Driven through TestClient(app) against the real test DB. Spawn tests rely on the
autouse `_local_spawn_in_process` fixture (tests/gateway/conftest.py) to run the
spawn op in-process, so a preset-seeded spawn actually persists its merged
`config_overlay`, which the test reads back through `db_conn`.
"""

from __future__ import annotations

import psycopg
from fastapi.testclient import TestClient

from gateway.app import app


def _create(client: TestClient, **kw: object):
    body = {"name": "coder", "label": "Coder", **kw}
    return client.post("/api/presets", json=body)


class TestPresetCrud:
    def test_create_returns_view(self, db_conn: psycopg.Connection) -> None:
        with TestClient(app) as client:
            r = _create(client, description="writes code", config={"llm_model": "claude-sonnet-5"})
        assert r.status_code == 201
        b = r.json()
        assert b["name"] == "coder"
        assert b["label"] == "Coder"
        assert b["description"] == "writes code"
        assert b["config"] == {"llm_model": "claude-sonnet-5"}
        assert isinstance(b["id"], int)

    def test_create_defaults_empty_config_and_null_description(
        self, db_conn: psycopg.Connection
    ) -> None:
        with TestClient(app) as client:
            r = _create(client, name="bare", label="Bare")
        assert r.status_code == 201
        b = r.json()
        assert b["config"] == {}
        assert b["description"] is None

    def test_create_duplicate_name_409(self, db_conn: psycopg.Connection) -> None:
        with TestClient(app) as client:
            assert _create(client, name="dup").status_code == 201
            assert _create(client, name="dup").status_code == 409

    def test_create_config_not_object_422(self, db_conn: psycopg.Connection) -> None:
        # `config` is a JSON object; a bare string / array is rejected by the
        # schema (this is the "must be valid JSON object" guard).
        with TestClient(app) as client:
            r = client.post(
                "/api/presets", json={"name": "x", "label": "X", "config": "notanobject"}
            )
        assert r.status_code == 422

    def test_create_rejects_cluster_consistent_framework_config(
        self, db_conn: psycopg.Connection
    ) -> None:
        with TestClient(app) as client:
            r = _create(client, config={"db_url": "postgresql://not-per-agent"})
        assert r.status_code == 422
        assert "not per_agent=True" in r.json()["detail"]

    def test_create_accepts_per_agent_and_opaque_config_keys(
        self, db_conn: psycopg.Connection
    ) -> None:
        with TestClient(app) as client:
            per_agent = _create(client, config={"llm_model": "claude-sonnet-5"})
            # Unknown keys may be plugin fields, so the gateway keeps them opaque.
            opaque = _create(
                client,
                name="plugin-config",
                label="Plugin config",
                config={"llm_modell": "x"},
            )
        assert per_agent.status_code == 201
        assert opaque.status_code == 201

    def test_create_blank_name_422(self, db_conn: psycopg.Connection) -> None:
        with TestClient(app) as client:
            r = client.post("/api/presets", json={"name": "", "label": "X"})
        assert r.status_code == 422

    def test_get_missing_404(self, db_conn: psycopg.Connection) -> None:
        with TestClient(app) as client:
            assert client.get("/api/presets/9999").status_code == 404

    def test_get_returns_view(self, db_conn: psycopg.Connection) -> None:
        with TestClient(app) as client:
            pid = _create(client, config={"a": 1}).json()["id"]
            b = client.get(f"/api/presets/{pid}").json()
        assert b["id"] == pid
        assert b["config"] == {"a": 1}

    def test_list_ordered_by_name(self, db_conn: psycopg.Connection) -> None:
        with TestClient(app) as client:
            _create(client, name="bbb", label="B")
            _create(client, name="aaa", label="A")
            rows = client.get("/api/presets").json()
        assert [r["name"] for r in rows] == ["aaa", "bbb"]

    def test_update_partial_leaves_other_fields(self, db_conn: psycopg.Connection) -> None:
        with TestClient(app) as client:
            pid = _create(client, config={"llm_model": "claude-sonnet-5"}).json()["id"]
            r = client.patch(
                f"/api/presets/{pid}",
                json={"label": "New", "config": {"llm_model": "deepseek-v4-pro"}},
            )
        assert r.status_code == 200
        b = r.json()
        assert b["label"] == "New"
        assert b["config"] == {"llm_model": "deepseek-v4-pro"}
        assert b["name"] == "coder"  # untouched field survives the partial update

    def test_update_no_fields_400(self, db_conn: psycopg.Connection) -> None:
        with TestClient(app) as client:
            pid = _create(client).json()["id"]
            assert client.patch(f"/api/presets/{pid}", json={}).status_code == 400

    def test_update_rejects_cluster_consistent_config_and_accepts_opaque_key(
        self, db_conn: psycopg.Connection
    ) -> None:
        with TestClient(app) as client:
            pid = _create(client).json()["id"]
            rejected = client.patch(
                f"/api/presets/{pid}", json={"config": {"db_url": "postgresql://not-per-agent"}}
            )
            opaque = client.patch(f"/api/presets/{pid}", json={"config": {"llm_modell": "x"}})
        assert rejected.status_code == 422
        assert opaque.status_code == 200

    def test_update_missing_404(self, db_conn: psycopg.Connection) -> None:
        with TestClient(app) as client:
            assert client.patch("/api/presets/9999", json={"label": "x"}).status_code == 404

    def test_update_duplicate_name_409(self, db_conn: psycopg.Connection) -> None:
        with TestClient(app) as client:
            _create(client, name="a", label="A")
            pid = _create(client, name="b", label="B").json()["id"]
            r = client.patch(f"/api/presets/{pid}", json={"name": "a"})
        assert r.status_code == 409

    def test_delete_then_404(self, db_conn: psycopg.Connection) -> None:
        with TestClient(app) as client:
            pid = _create(client).json()["id"]
            assert client.delete(f"/api/presets/{pid}").status_code == 200
            assert client.get(f"/api/presets/{pid}").status_code == 404
            assert client.delete(f"/api/presets/{pid}").status_code == 404


class TestSpawnWithPreset:
    def _overlay(self, db_conn: psycopg.Connection, agent_id: int) -> dict:
        with db_conn.cursor() as cur:
            cur.execute("SELECT config_overlay FROM agents_meta WHERE id = %s", (agent_id,))
            row = cur.fetchone()
        assert row is not None
        return row[0]

    def test_preset_seeds_config_and_explicit_wins(self, db_conn: psycopg.Connection) -> None:
        with TestClient(app) as client:
            _create(
                client,
                name="coder",
                label="Coder",
                config={
                    "llm_model": "claude-sonnet-5",
                    "skills_to_inject_into_system_prompt": ["a"],
                },
            )
            r = client.post(
                "/api/agents",
                json={
                    "spawner": "user",
                    "preset": "coder",
                    "config": {"llm_model": "deepseek-v4-pro"},
                },
            )
            assert r.status_code == 201, r.text
            agent_id = r.json()["id"]
        # preset supplies skills_to_inject; the explicit llm_model overrides the
        # preset's per-key.
        assert self._overlay(db_conn, agent_id) == {  # pyright: ignore[reportUnknownMemberType]
            "llm_model": "deepseek-v4-pro",
            "skills_to_inject_into_system_prompt": ["a"],
        }

    def test_preset_only_seeds_full_config(self, db_conn: psycopg.Connection) -> None:
        with TestClient(app) as client:
            _create(client, name="coder", label="Coder", config={"llm_model": "claude-sonnet-5"})
            r = client.post("/api/agents", json={"spawner": "user", "preset": "coder"})
            assert r.status_code == 201, r.text
            agent_id = r.json()["id"]
        assert self._overlay(db_conn, agent_id) == {"llm_model": "claude-sonnet-5"}  # pyright: ignore[reportUnknownMemberType]

    def test_unknown_preset_400(self, db_conn: psycopg.Connection) -> None:
        with TestClient(app) as client:
            r = client.post("/api/agents", json={"spawner": "user", "preset": "ghost"})
        assert r.status_code == 400
        assert "ghost" in r.json()["detail"]
