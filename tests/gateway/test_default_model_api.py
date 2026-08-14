"""GET/PUT /api/config/default-model — the cluster's default model.

A narrow endpoint on purpose (see gateway/routers/default_model.py): the value
does not live in `.env`, and the full-replace `PUT /api/config` reducer has no
business touching it. These tests pin the two things that make it safe to expose
in the panel — the roster check, and that a write never reaches an existing agent.

`cluster_defaults` is a seeded singleton outside the per-test TRUNCATE, so the
fixture restores NULL.
"""

from __future__ import annotations

import psycopg
import pytest
from fastapi.testclient import TestClient

from gateway.app import app


def _spawn_agent(spawner: str = "test") -> int:
    """Setup helper — a row with a stamped birth_config (the #1236 split: the
    row is created by create_agent_row; nothing launches, these tests only read
    the stamp)."""
    from ops.agent_spawn import create_agent_row
    from shared.machine import machine_name

    agent_id, _ = create_agent_row(spawner=spawner, machine=machine_name())
    return agent_id


@pytest.fixture(autouse=True)
def _unset(cluster_defaults_unset: None) -> None:
    """Every test here starts from "no cluster choice" (see the shared fixture)."""


class TestGet:
    def test_unset_reports_the_config_chain(self) -> None:
        from shared.config import settings

        with TestClient(app) as client:
            resp = client.get("/api/config/default-model")
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"model": settings.lm.llm_model, "source": "config"}

    def test_reports_the_cluster_row_once_set(self) -> None:
        with TestClient(app) as client:
            client.put("/api/config/default-model", json={"model": "claude-sonnet-5"})
            resp = client.get("/api/config/default-model")
        assert resp.json() == {"model": "claude-sonnet-5", "source": "cluster"}


class TestPut:
    def test_accepts_a_spawnable_model(self) -> None:
        with TestClient(app) as client:
            resp = client.put("/api/config/default-model", json={"model": "deepseek-v4-flash"})
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"model": "deepseek-v4-flash", "source": "cluster"}

    def test_rejects_an_unknown_model(self) -> None:
        """Fail fast at the write site: a bad id stored here would only surface at a
        far-away spawn."""
        with TestClient(app) as client:
            resp = client.put("/api/config/default-model", json={"model": "gpt-9-imaginary"})
        assert resp.status_code == 400
        assert "gpt-9-imaginary" in resp.json()["detail"]

    def test_a_rejected_write_stores_nothing(self, db_conn: psycopg.Connection) -> None:
        with TestClient(app) as client:
            client.put("/api/config/default-model", json={"model": "gpt-9-imaginary"})
        with db_conn.cursor() as cur:
            cur.execute("SELECT llm_model FROM cluster_defaults WHERE id = 1")
            row = cur.fetchone()
        assert row is not None
        assert row[0] is None

    def test_does_not_move_an_existing_agent(self, db_conn: psycopg.Connection) -> None:
        """The panel control is safe to use on a live cluster."""
        agent_id = _spawn_agent(spawner="test")
        with db_conn.cursor() as cur:
            cur.execute("SELECT birth_config FROM agents_meta WHERE id = %s", (agent_id,))
            row = cur.fetchone()
        assert row is not None
        born_with = row[0]["llm_model"]

        with TestClient(app) as client:
            client.put("/api/config/default-model", json={"model": "claude-sonnet-5"})

        with db_conn.cursor() as cur:
            cur.execute("SELECT birth_config FROM agents_meta WHERE id = %s", (agent_id,))
            row = cur.fetchone()
        assert row is not None
        assert row[0]["llm_model"] == born_with
