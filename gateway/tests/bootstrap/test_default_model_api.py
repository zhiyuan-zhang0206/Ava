"""GET/PUT /api/config/default-model — the cluster's default model.

A narrow endpoint on purpose (see gateway/routers/default_model.py): the value
does not live in `.env`, and the full-replace `PUT /api/config` reducer has no
business touching it. These tests pin the two things that make it safe to expose
in the panel — the roster check, and that a write never reaches an existing agent.

`cluster_defaults` is a seeded singleton outside the per-test TRUNCATE, so the
fixture restores NULL.
"""

from __future__ import annotations

from dataclasses import replace

import psycopg
import pytest
from fastapi.testclient import TestClient

from base.db import Database
from base.events.live.bus import EventBus
from base.lm.plugin_providers import model_catalog
from gateway.app import app
from tests.fixtures.model_catalog import AddModels


@pytest.fixture
def withdrawn_model(add_models: AddModels) -> str:
    model = "deepseek-retired-fixture"
    base = model_catalog().models["deepseek-flash"]
    add_models({model: replace(base, spawnable=False, unavailable_fallback="deepseek-flash")})
    return model


def _spawn_agent(spawner: str = "test") -> int:
    """Setup helper — a row with a stamped birth_config (the #1236 split: the
    row is created by create_agent_row; nothing launches, these tests only read
    the stamp)."""
    from base.cluster.machine import machine_name
    from ops.agents.spawn import create_agent_row

    agent_id, _, _prompt_id, _attempt_id = create_agent_row(
        Database.from_settings(), EventBus.from_settings(), spawner=spawner, machine=machine_name()
    )
    return agent_id


@pytest.fixture(autouse=True)
def _unset(cluster_defaults_unset: None) -> None:
    """Every test here starts from "no cluster choice" (see the shared fixture)."""


class TestGet:
    def test_unset_reports_the_config_chain(self) -> None:
        from base.config import settings

        with TestClient(app) as client:
            resp = client.get("/api/config/default-model")
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"model": settings.lm.llm_model, "source": "config"}

    def test_reports_the_cluster_row_once_set(self) -> None:
        with TestClient(app) as client:
            client.put("/api/config/default-model", json={"model": "claude-sonnet-5"})
            resp = client.get("/api/config/default-model")
        assert resp.json() == {"model": "claude-sonnet-5", "source": "cluster"}

    def test_unset_resolves_a_withdrawn_config_model(
        self, monkeypatch: pytest.MonkeyPatch, withdrawn_model: str
    ) -> None:
        """A config chain naming a withdrawn id reports what actually runs: the
        spawn boundary resolves it the same way (`factory.validate_model_config`)."""
        from base.config import settings

        monkeypatch.setattr(settings.lm, "llm_model", withdrawn_model)
        with TestClient(app) as client:
            resp = client.get("/api/config/default-model")
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"model": "deepseek-flash", "source": "config"}

    def test_resolves_a_withdrawn_cluster_row(
        self, db_conn: psycopg.Connection, withdrawn_model: str
    ) -> None:
        """A row written while its model was still spawnable keeps the id; the
        endpoint still answers with the model a new agent actually runs."""
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE cluster_defaults SET llm_model = %s WHERE id = 1", (withdrawn_model,)
            )
        db_conn.commit()
        with TestClient(app) as client:
            resp = client.get("/api/config/default-model")
            picker = client.get("/api/models")
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"model": "deepseek-flash", "source": "cluster"}
        assert picker.json()["default"] == "deepseek-flash"


class TestPut:
    def test_picker_preflight_and_birth_use_the_cluster_default(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from base.config import settings

        # MiMo cannot accept max: checking the old config default would reject
        # this valid DeepSeek birth before it could be persisted.
        monkeypatch.setattr(settings.lm, "llm_model", "mimo-v2.6-pro")
        with TestClient(app) as client:
            saved = client.put("/api/config/default-model", json={"model": "deepseek-flash"})
            assert saved.status_code == 200, saved.text
            assert client.get("/api/models").json()["default"] == "deepseek-flash"
            spawned = client.post("/api/agents", json={"config": {"reasoning_effort": "max"}})
        assert spawned.status_code == 201, spawned.text
        row = db_conn.execute(
            "SELECT config_overlay,birth_config FROM agents_meta WHERE id=%s",
            (spawned.json()["id"],),
        ).fetchone()
        assert row is not None
        assert row[0] == {"reasoning_effort": "max"}
        assert row[1]["llm_model"] == "deepseek-flash"

    def test_accepts_a_spawnable_model(self) -> None:
        with TestClient(app) as client:
            resp = client.put("/api/config/default-model", json={"model": "deepseek-flash"})
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"model": "deepseek-flash", "source": "cluster"}

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
