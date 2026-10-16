from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from typing import Any, cast

import psycopg
import pytest
from fastapi.testclient import TestClient

from base.lm.catalog import ModelCatalog
from base.packages.plugin_providers import build_model_catalog
from base.packages.tests.model_catalog import AddModels
from tests.fixtures.gateway_config import gateway_test_client, spawn_via_http


@pytest.fixture
def withdrawn_model(add_models: AddModels, model_catalog: ModelCatalog) -> tuple[str, ModelCatalog]:
    model = "deepseek-retired-fixture"
    base = model_catalog.models["deepseek-flash"]
    catalog = add_models(
        model_catalog,
        {model: replace(base, spawnable=False, unavailable_fallback="deepseek-flash")},
    )
    return model, catalog


def _agent_row(db: psycopg.Connection, agent_id: int) -> tuple | None:
    with db.cursor() as cur:
        cur.execute(
            "SELECT id, spawner, fork_source_agent_id, "
            "fork_source_checkpoint_id, status FROM agents_meta WHERE id = %s",
            (agent_id,),
        )
        return cur.fetchone()


def returned_id(cur: psycopg.Cursor) -> int:
    row = cur.fetchone()
    assert row is not None
    return cast(int, row[0])


def inbound_rows(db: psycopg.Connection, agent_id: int) -> list[tuple[str, str, str]]:
    with db.cursor() as cur:
        cur.execute(
            "SELECT content, kind, source FROM inbound_messages WHERE agent_id = %s ORDER BY id",
            (agent_id,),
        )
        return cur.fetchall()


def terminate_hosted(db: psycopg.Connection, agent_id: int) -> None:
    db.execute(
        "UPDATE agents_meta SET status = 'terminated', runtime_kind = 'hosted', "
        "runtime_generation = gen_random_uuid(), runtime_owner = gen_random_uuid() WHERE id = %s",
        (agent_id,),
    )
    db.commit()


def _flat_model_ids(body: dict[str, Any]) -> list[str]:
    return [m for group in body["providers"].values() for m in group]


def _assert_retired_models_absent(body: dict[str, Any], flat: list[str]) -> None:
    for model in (
        "deepseek-v4-pro",
        "deepseek-v4-flash",
        "deepseek-v4-flash-vision-exp",
        "mimo-v2.5-pro-ultraspeed",
    ):
        assert model not in flat
        assert model not in body["models"]


def test_get_models_returns_grouped_supported_models() -> None:
    with gateway_test_client() as client:
        resp = client.get("/api/models")
    assert resp.status_code == 200
    body = resp.json()
    flat = _flat_model_ids(body)
    assert "deepseek-flash" in flat
    _assert_retired_models_absent(body, flat)
    assert "gpt-5.6-sol" in flat
    assert "claude-sonnet-5" in flat
    assert "gpt-5.6-terra" in flat
    assert "gpt-5.6-luna" in flat
    assert body["models"]["gpt-5.6-sol"]["pricing"] == {
        "input": 4.0,
        "cache_read": 0.4,
        "output": 20.0,
        "cache_write_5m": None,
        "cache_write_1h": None,
    }
    from base.config import settings

    assert body["default"] == settings.lm.llm_model


def test_get_models_surfaces_superseded_by(
    add_models: AddModels,
    model_catalog: ModelCatalog,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    glm = model_catalog.models["glm-5.2"]
    catalog = add_models(model_catalog, {"glm-5.2": replace(glm, superseded_by="kimi-k3")})
    import gateway.app as gateway_app

    monkeypatch.setattr(gateway_app, "build_model_catalog", lambda: catalog)
    with gateway_test_client() as client:
        resp = client.get("/api/models")
    body = resp.json()
    assert body["models"]["glm-5.2"]["superseded_by"] == "kimi-k3"
    assert body["models"]["deepseek-flash"]["superseded_by"] is None


def test_get_models_every_model_has_reasoning_effort_control() -> None:
    with gateway_test_client() as client:
        resp = client.get("/api/models")
    body = resp.json()
    missing = [
        model for model, info in body["models"].items() if info["reasoning_effort_options"] is None
    ]
    assert missing == [], f"models with no reasoning effort control: {missing}"


def _assert_provider_binding_vocabulary(
    models: dict[str, Any], provider: str, binding: str
) -> None:
    binding_levels = build_model_catalog().bindings[binding].effort_levels
    assert binding_levels is not None
    provider_models = [m for m, info in models.items() if info["provider"] == provider]
    assert provider_models, f"no {provider} models registered for the binding check"
    for model in provider_models:
        assert models[model]["reasoning_effort_options"] == list(binding_levels), model


def _assert_provider_vocabulary_subset(models: dict[str, Any], provider: str, binding: str) -> None:
    binding_levels = build_model_catalog().bindings[binding].effort_levels
    assert binding_levels is not None
    fallback = set(binding_levels)
    provider_models = [m for m, info in models.items() if info["provider"] == provider]
    assert provider_models, f"no {provider} models registered for the subset check"
    for model in provider_models:
        options = set(models[model]["reasoning_effort_options"])
        assert options <= fallback, model


def test_get_models_reasoning_effort_options_match_factory_tables() -> None:
    with gateway_test_client() as client:
        resp = client.get("/api/models")
    models = resp.json()["models"]

    for model, info in models.items():
        expected_levels = build_model_catalog().models[model].effort_levels
        assert expected_levels is not None, model
        assert info["reasoning_effort_options"] == list(expected_levels), model

    _assert_provider_binding_vocabulary(models, "mimo", "mimo-")
    _assert_provider_binding_vocabulary(models, "kimi", "kimi-")
    _assert_provider_binding_vocabulary(models, "glm", "glm-")
    _assert_provider_binding_vocabulary(models, "qwen", "qwen3.8-")
    _assert_provider_vocabulary_subset(models, "gemini", "gemini-")

    assert models["claude-haiku-4-5-20251001"]["reasoning_effort_options"] == ["none", "high"]


def test_get_models_reasoning_effort_default_is_the_per_model_tuning_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from base.config import settings

    with gateway_test_client() as client:
        resp = client.get("/api/models")
    assert resp.status_code == 200
    models = resp.json()["models"]

    assert models["deepseek-flash"]["reasoning_effort_default"] == "max"
    assert models["claude-sonnet-5"]["reasoning_effort_default"] == "high"
    assert models["claude-haiku-4-5-20251001"]["reasoning_effort_default"] == "none"
    assert models["gpt-5.6-sol"]["reasoning_effort_default"] == "medium"
    assert models["kimi-k3"]["reasoning_effort_default"] == "max"
    assert models["glm-5.2"]["reasoning_effort_default"] == "max"

    for model, info in models.items():
        expected = build_model_catalog().models[model].tuning.reasoning_effort
        assert info["reasoning_effort_default"] == expected, model
        assert expected, model
        assert expected in info["reasoning_effort_options"], model

    monkeypatch.setattr(settings.lm, "reasoning_effort", "low")
    with gateway_test_client() as client:
        resp = client.get("/api/models")
    models = resp.json()["models"]
    assert models["deepseek-flash"]["reasoning_effort_default"] == "max"


class TestSpawn:
    def test_spawn_minimal_no_prompt_no_spawner(self, db_conn: psycopg.Connection) -> None:
        with gateway_test_client() as client:
            resp = spawn_via_http(client, {})
        assert resp.status_code == 201
        new_id = resp.json()["id"]
        row = _agent_row(db_conn, new_id)
        assert row == (new_id, "user", None, None, "idling")
        assert inbound_rows(db_conn, new_id) == []

    def test_spawn_with_prompt_inserts_chat_inbound(self, db_conn: psycopg.Connection) -> None:
        with gateway_test_client() as client:
            resp = spawn_via_http(client, {"prompt": "\u67e5 X", "prompt_source": "user"})
        new_id = resp.json()["id"]
        assert inbound_rows(db_conn, new_id) == [("\u67e5 X", "chat", "user")]

    def test_spawn_with_long_prompt_inserts_chat_inbound(self, db_conn: psycopg.Connection) -> None:
        prompt = "a" * 100_000
        with gateway_test_client() as client:
            resp = spawn_via_http(client, {"prompt": prompt, "prompt_source": "user"})
        assert resp.status_code == 201
        new_id = resp.json()["id"]
        assert inbound_rows(db_conn, new_id) == [(prompt, "chat", "user")]

    def test_spawn_with_explicit_spawner(self, db_conn: psycopg.Connection) -> None:
        with gateway_test_client() as client:
            resp = spawn_via_http(client, {"spawner": "claude-code"})
        new_id = resp.json()["id"]
        row = _agent_row(db_conn, new_id)
        assert row is not None and row[1] == "claude-code"

    def test_spawn_settles_withdrawn_model_and_returns_receipt(
        self,
        db_conn: psycopg.Connection,
        withdrawn_model: tuple[str, ModelCatalog],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        model, catalog = withdrawn_model
        import gateway.app as gateway_app

        monkeypatch.setattr(gateway_app, "build_model_catalog", lambda: catalog)
        with gateway_test_client() as client:
            resp = spawn_via_http(client, {"config": {"llm_model": model}})
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["config_normalized"] == {
            "requested": model,
            "resolved": "deepseek-flash",
        }
        with db_conn.cursor() as cur:
            cur.execute("SELECT config_overlay FROM agents_meta WHERE id = %s", (body["id"],))
            assert cur.fetchone() == ({"llm_model": "deepseek-flash"},)

    def test_spawn_available_model_carries_no_receipt(self, db_conn: psycopg.Connection) -> None:
        with gateway_test_client() as client:
            resp = spawn_via_http(client, {"config": {"llm_model": "deepseek-flash"}})
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body.get("config_normalized") is None
        with db_conn.cursor() as cur:
            cur.execute("SELECT config_overlay FROM agents_meta WHERE id = %s", (body["id"],))
            assert cur.fetchone() == ({"llm_model": "deepseek-flash"},)

    def test_spawn_fork_resolves_latest_and_copies_checkpoint(
        self, db_conn: psycopg.Connection
    ) -> None:
        with gateway_test_client() as client:
            source = spawn_via_http(client, {}).json()["id"]
            with db_conn.cursor() as cur:
                for ckpt, parent in [("cka", None), ("ckb", "cka"), ("ckc", "ckb")]:
                    cur.execute(
                        "INSERT INTO checkpoints (thread_id, checkpoint_id, parent_checkpoint_id, "
                        "checkpoint, metadata) VALUES (%s, %s, %s, '{}'::jsonb, '{}'::jsonb)",
                        (str(source), ckpt, parent),
                    )
            db_conn.commit()

            resp = spawn_via_http(client, {"fork_from": source})
        new_id = resp.json()["id"]
        row = _agent_row(db_conn, new_id)
        assert row is not None
        assert row[2] == source and row[3] == "ckc"
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT checkpoint_id FROM checkpoints WHERE thread_id = %s ORDER BY checkpoint_id",
                (str(new_id),),
            )
            ckpts = [r[0] for r in cur.fetchall()]
        assert ckpts == ["cka", "ckb", "ckc"]

    def test_spawn_fork_source_no_checkpoint_returns_409(
        self,
        db_conn: psycopg.Connection,
    ) -> None:
        with gateway_test_client() as client:
            source = spawn_via_http(client, {}).json()["id"]
            resp = spawn_via_http(client, {"fork_from": source})
        assert resp.status_code == 409
        body = resp.json()
        assert body["reason"] == "fork_source_empty"
        assert "no checkpoint" in body["detail"]

    def test_spawn_empty_prompt_validation(
        self,
        db_conn: psycopg.Connection,
    ) -> None:
        with gateway_test_client() as client:
            resp = spawn_via_http(client, {"prompt": "   "})
        assert resp.status_code == 422

    def test_spawn_prompt_without_prompt_source_returns_422(
        self,
        db_conn: psycopg.Connection,
    ) -> None:
        with gateway_test_client() as client:
            resp = spawn_via_http(client, {"prompt": "\u67e5 X"})
        assert resp.status_code == 422
        assert "prompt_source" in resp.text


def test_post_commit_unknown_launch_failure_carries_id_and_cors_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import gateway.agents.creation as _agents_creation
    from ops.rpc_schemas import LaunchAgentRequest, SpawnedAgent

    async def _explode(_db: object, target: str, body: LaunchAgentRequest) -> SpawnedAgent:
        raise RuntimeError("boom")

    monkeypatch.setattr(_agents_creation, "forward_spawn_to_remote", _explode)
    with gateway_test_client() as client:
        allowed_origin = client.app.state.gateway_config.allowed_origins[0]
        resp = spawn_via_http(client, {}, headers={"Origin": allowed_origin})
    assert resp.status_code == 502
    assert resp.json()["reason"] == "agent_launch_failed"
    assert resp.json()["agent_id"] > 0
    assert resp.json()["state"]["availability"]["reason"] == "launch_unknown"
    assert resp.headers["access-control-allow-origin"] == allowed_origin
    assert resp.headers["access-control-allow-credentials"] == "true"


class TestTerminate:
    def test_terminate_inserts_inbound_and_returns_enqueued(
        self, db_conn: psycopg.Connection
    ) -> None:
        with gateway_test_client() as client:
            agent_id = spawn_via_http(client, {}).json()["id"]
            resp = client.post(f"/api/agents/{agent_id}/terminate")
        assert resp.status_code == 200
        assert resp.json() == {"status": "enqueued", "open_tasks": None, "shell_sessions": None}
        assert inbound_rows(db_conn, agent_id) == [("", "terminate", "user")]

    def test_terminate_already_terminated_is_noop(self, db_conn: psycopg.Connection) -> None:
        with gateway_test_client() as client:
            agent_id = spawn_via_http(client, {}).json()["id"]
            with db_conn.cursor() as cur:
                cur.execute(
                    "UPDATE agents_meta SET status = 'terminated' WHERE id = %s", (agent_id,)
                )
            db_conn.commit()
            resp = client.post(f"/api/agents/{agent_id}/terminate")
        assert resp.status_code == 200
        assert resp.json() == {
            "status": "already_terminated",
            "open_tasks": None,
            "shell_sessions": None,
        }
        assert inbound_rows(db_conn, agent_id) == []

    def test_terminate_nonexistent_404(
        self,
        db_conn: psycopg.Connection,
    ) -> None:
        with gateway_test_client() as client:
            resp = client.post("/api/agents/9999/terminate")
        assert resp.status_code == 404
        assert "does not exist" in resp.json()["detail"]

    def test_terminate_force_commits_fence_and_returns_enqueued(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with gateway_test_client() as client:
            agent_id = spawn_via_http(client, {}).json()["id"]
            resp = client.post(f"/api/agents/{agent_id}/terminate", json={"force": True})
        assert resp.status_code == 200
        assert resp.json() == {"status": "enqueued", "open_tasks": None, "shell_sessions": None}

        row = _agent_row(db_conn, agent_id)
        assert row is not None and row[4] == "terminated"

        assert inbound_rows(db_conn, agent_id) == [("", "terminate", "user")]

    def test_terminate_force_with_custom_source(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with gateway_test_client() as client:
            agent_id = spawn_via_http(client, {}).json()["id"]
            resp = client.post(
                f"/api/agents/{agent_id}/terminate",
                json={"force": True, "source": "agent:42"},
            )
        assert resp.json() == {"status": "enqueued", "open_tasks": None, "shell_sessions": None}

        rows = inbound_rows(db_conn, agent_id)
        assert rows == [("", "terminate", "agent:42")]

    def test_terminate_force_already_terminated_returns_already_terminated(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with gateway_test_client() as client:
            agent_id = spawn_via_http(client, {}).json()["id"]
            with db_conn.cursor() as cur:
                cur.execute(
                    "UPDATE agents_meta SET status = 'terminated', pid = 424242 WHERE id = %s",
                    (agent_id,),
                )
            db_conn.commit()
            with db_conn.cursor() as cur:
                cur.execute(
                    "SELECT status_changed_at FROM agents_meta WHERE id = %s",
                    (agent_id,),
                )
                before = cur.fetchone()
            resp = client.post(f"/api/agents/{agent_id}/terminate", json={"force": True})
        assert resp.json() == {"status": "enqueued", "open_tasks": None, "shell_sessions": None}
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT status_changed_at, last_force_terminate_inbound_id "
                "FROM agents_meta WHERE id = %s",
                (agent_id,),
            )
            after = cur.fetchone()
        assert before is not None and after is not None
        assert after[0] == before[0]
        assert after[1] is not None
        assert inbound_rows(db_conn, agent_id) == [("", "terminate", "user")]

    def test_terminate_force_nonexistent_404(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with gateway_test_client() as client:
            resp = client.post("/api/agents/9999/terminate", json={"force": True})
        assert resp.status_code == 404
        assert "does not exist" in resp.json()["detail"]


class TestAutoResurrect:
    def test_chat_to_terminated_agent_triggers_auto_resurrect(
        self, db_conn: psycopg.Connection
    ) -> None:
        with gateway_test_client() as client:
            agent_id = spawn_via_http(client, {}).json()["id"]
            terminate_hosted(db_conn, agent_id)
            resp = client.post(
                f"/api/agents/{agent_id}/messages",
                json={"content": "resume your work", "source": "user"},
            )
        assert resp.status_code == 201
        rows = inbound_rows(db_conn, agent_id)
        assert ("", "resurrect", "system") in rows
        assert ("resume your work", "chat", "user") in rows

    def test_chat_to_alive_agent_no_resurrect(self, db_conn: psycopg.Connection) -> None:
        with gateway_test_client() as client:
            agent_id = spawn_via_http(client, {}).json()["id"]
            resp = client.post(
                f"/api/agents/{agent_id}/messages",
                json={"content": "hello", "source": "user"},
            )
        assert resp.status_code == 201
        rows = inbound_rows(db_conn, agent_id)
        assert ("hello", "chat", "user") in rows
        resurrect_rows = [r for r in rows if r[1] == "resurrect"]
        assert len(resurrect_rows) == 0

    def test_chat_illegal_source_rejected_422(self, db_conn: psycopg.Connection) -> None:
        with gateway_test_client() as client:
            agent_id = spawn_via_http(client, {}).json()["id"]
            resp = client.post(
                f"/api/agents/{agent_id}/messages",
                json={"content": "hello", "source": "ui:web"},
            )
        assert resp.status_code == 422


_RESULT_READ_ENDPOINTS = [
    ("GET", "/api/agents/{agent_id}/messages"),
    ("GET", "/api/agents/{agent_id}/traces/trace-1/messages"),
    ("GET", "/api/agents/{agent_id}/last-message"),
    ("GET", "/api/agents/{agent_id}/pending"),
    ("GET", "/api/agents/{agent_id}/timeline"),
    ("GET", "/api/agents/{agent_id}/events"),
    ("GET", "/api/agents/{agent_id}/events/stream"),
    ("GET", "/api/events"),
    ("POST", "/api/memory/search"),
    ("GET", "/api/tasks"),
]


def _result_read(client: TestClient, method: str, path: str, *, caller: str | None = None):
    params = {"caller": caller} if caller is not None else None
    if method == "POST":
        return client.post(path, params=params, json={"query": "test", "k": 1, "purpose": "search"})
    return client.get(path, params=params)


def _stub_result_read_backends(monkeypatch: pytest.MonkeyPatch) -> None:
    import gateway.events.agent_events as agent_events_router
    from services.derived.memory_indexer.backends import factory as backend_factory
    from services.derived.memory_indexer.embeddings import factory as _embedding_factory

    class _StubProvider:
        dim = 8
        fingerprint = "fake:provider:dim=8"

        def __init__(
            self,
            name: str,
            *,
            catalog: ModelCatalog,
            timeout_reader: Callable[[], float],
            api_key_reader: Callable[[], str | None],
        ) -> None:
            assert name
            self.catalog = catalog
            self.timeout_reader = timeout_reader
            self.api_key_reader = api_key_reader

        @staticmethod
        async def embed_query_async(_query: str) -> list[float]:
            return [0.0] * 8

    class StubBackend:
        async def search_topk_async(self, *_args: object, **_kwargs: object) -> list[object]:
            return []

    def backend(*_args: object, **_kwargs: object) -> StubBackend:
        return StubBackend()

    async def _stream(*_args: object, **_kwargs: object):
        if False:
            yield ""

    monkeypatch.setattr(_embedding_factory, "get_provider", _StubProvider)
    monkeypatch.setattr(backend_factory, "get_backend", backend)
    monkeypatch.setattr(agent_events_router, "event_stream", _stream)


@pytest.mark.parametrize(("method", "path_template"), _RESULT_READ_ENDPOINTS)
def test_eval_isolated_callers_cannot_read_result_surfaces(
    db_conn: psycopg.Connection, method: str, path_template: str
) -> None:
    from base.db import create_agent

    target_id = create_agent(db_conn)
    caller_id = create_agent(db_conn)
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agents_meta (id, spawner, status) VALUES (%s, 'test', 'running')",
            (target_id,),
        )
        cur.execute(
            "INSERT INTO agents_meta (id, spawner, status, config_overlay) "
            "VALUES (%s, 'test', 'running', %s::jsonb)",
            (caller_id, json.dumps({"eval_isolation": True})),
        )
    db_conn.commit()

    with gateway_test_client() as client:
        resp = _result_read(
            client,
            method,
            path_template.format(agent_id=target_id),
            caller=f"agent:{caller_id}",
        )

    assert resp.status_code == 403
    assert "eval-isolated" in resp.json()["detail"]
    if path_template.endswith("/last-message"):
        assert resp.json()["detail"] == (
            f"caller agent {caller_id} is eval-isolated: last-message reads are denied"
        )


@pytest.mark.parametrize(("method", "path_template"), _RESULT_READ_ENDPOINTS)
def test_result_surfaces_allow_non_isolated_and_unmarked_callers(
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    path_template: str,
) -> None:
    from base.db import create_agent

    _stub_result_read_backends(monkeypatch)
    target_id = create_agent(db_conn)
    caller_id = create_agent(db_conn)
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agents_meta (id, spawner, status) VALUES (%s, 'test', 'running')",
            (target_id,),
        )
        cur.execute(
            "INSERT INTO agents_meta (id, spawner, status) VALUES (%s, 'test', 'running')",
            (caller_id,),
        )
    db_conn.commit()
    path = path_template.format(agent_id=target_id)

    with gateway_test_client() as client:
        ordinary = _result_read(client, method, path, caller=f"agent:{caller_id}")
        unmarked = _result_read(client, method, path)

    assert ordinary.status_code == 200
    assert unmarked.status_code == (422 if path_template.endswith("/last-message") else 200)


def test_get_models_haiku_55_picker_contract() -> None:
    with gateway_test_client() as client:
        response = client.get("/api/models")
    assert response.status_code == 200
    models = response.json()["models"]
    assert models["claude-haiku-5-5"]["reasoning_effort_default"] == "medium"
    assert models["claude-haiku-5-5"]["reasoning_effort_options"] == [
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
    ]
