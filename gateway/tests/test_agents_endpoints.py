"""Gateway lifecycle endpoints HTTP integration tests.

POST /api/agents (spawn + optional prompt + optional fork_from)
POST /api/agents/{id}/terminate (INSERT terminate inbound)
GET /api/agents (bounded directory cards and explicit cursor)

Real database rows and gateway/home-runner operation dispatch; no host task is started.
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any, cast

import psycopg
import pytest
from fastapi.testclient import TestClient

from base.lm.plugin_providers import model_catalog
from gateway.app import app
from gateway.auth.cors import cors_allowed_origins
from tests.fixtures.model_catalog import AddModels


@pytest.fixture
def withdrawn_model(add_models: AddModels) -> str:
    model = "deepseek-retired-fixture"
    base = model_catalog().models["deepseek-flash"]
    add_models({model: replace(base, spawnable=False, unavailable_fallback="deepseek-flash")})
    return model


def _agent_row(db: psycopg.Connection, agent_id: int) -> tuple | None:
    with db.cursor() as cur:
        cur.execute(
            "SELECT id, spawner, fork_source_agent_id, "
            "fork_source_checkpoint_id, status FROM agents_meta WHERE id = %s",
            (agent_id,),
        )
        return cur.fetchone()


def _returned_id(cur: psycopg.Cursor) -> int:
    """Read an integer primary key from a RETURNING cursor."""
    row = cur.fetchone()
    assert row is not None
    return cast(int, row[0])


def _inbound_rows(db: psycopg.Connection, agent_id: int) -> list[tuple[str, str, str]]:
    with db.cursor() as cur:
        cur.execute(
            "SELECT content, kind, source FROM inbound_messages WHERE agent_id = %s ORDER BY id",
            (agent_id,),
        )
        return cur.fetchall()


def _terminate_hosted(db: psycopg.Connection, agent_id: int) -> None:
    """Terminate a hosted incarnation; resurrection resumes only retained hosted authority."""
    db.execute(
        "UPDATE agents_meta SET status = 'terminated', runtime_kind = 'hosted', "
        "runtime_generation = gen_random_uuid(), runtime_owner = gen_random_uuid() WHERE id = %s",
        (agent_id,),
    )
    db.commit()


def _flat_model_ids(body: dict[str, Any]) -> list[str]:
    return [m for group in body["providers"].values() for m in group]


def _assert_retired_models_absent(body: dict[str, Any], flat: list[str]) -> None:
    # Retired ids are absent from the entire registry, including the picker.
    for model in (
        "deepseek-v4-pro",
        "deepseek-v4-flash",
        "deepseek-v4-flash-vision-exp",
        "mimo-v2.5-pro-ultraspeed",
    ):
        assert model not in flat
        assert model not in body["models"]


def test_get_models_returns_grouped_supported_models() -> None:
    with TestClient(app) as client:
        resp = client.get("/api/models")
    assert resp.status_code == 200
    body = resp.json()
    flat = _flat_model_ids(body)
    assert "deepseek-flash" in flat
    _assert_retired_models_absent(body, flat)
    assert "gpt-5.6-sol" in flat
    # additional verified-live models
    assert "claude-sonnet-5" in flat
    assert "gpt-5.6-terra" in flat
    assert "gpt-5.6-luna" in flat
    assert body["models"]["gpt-5.6-sol"]["pricing"] == {
        "input": 4.0,
        "cache_read": 0.4,
        "output": 20.0,
    }
    from base.config import settings

    assert body["default"] == settings.lm.llm_model


def test_get_models_surfaces_superseded_by(
    add_models: AddModels,
) -> None:
    """The picker's hide-by-default rule is data, not gateway logic: the
    endpoint publishes each model's ``superseded_by`` straight off the registry,
    and an un-superseded model carries null."""
    glm = model_catalog().models["glm-5.2"]
    add_models({"glm-5.2": replace(glm, superseded_by="kimi-k3")})
    with TestClient(app) as client:
        resp = client.get("/api/models")
    body = resp.json()
    assert body["models"]["glm-5.2"]["superseded_by"] == "kimi-k3"
    assert body["models"]["deepseek-flash"]["superseded_by"] is None


def test_get_models_every_model_has_reasoning_effort_control() -> None:
    """No model may render a bare "Effort: default" blank dropdown — every
    provider either exposes a graded reasoning_effort field, or, where the
    real API only has a binary thinking on/off switch (mimo,
    claude-haiku-4-5-20251001), a two-value control mapped onto that switch. Locks in
    the 2026-07-24 audit that closed the mimo/haiku gap (both used to return
    `reasoning_effort_options: null`, hiding the dropdown entirely)."""
    with TestClient(app) as client:
        resp = client.get("/api/models")
    body = resp.json()
    missing = [
        model for model, info in body["models"].items() if info["reasoning_effort_options"] is None
    ]
    assert missing == [], f"models with no reasoning effort control: {missing}"


def _assert_provider_binding_vocabulary(
    models: dict[str, Any], provider: str, binding: str
) -> None:
    """The provider's models all publish the binding's wire-clamp vocabulary."""
    binding_levels = model_catalog().bindings[binding].effort_levels
    assert binding_levels is not None
    provider_models = [m for m, info in models.items() if info["provider"] == provider]
    assert provider_models, f"no {provider} models registered for the binding check"
    for model in provider_models:
        assert models[model]["reasoning_effort_options"] == list(binding_levels), model


def _assert_provider_vocabulary_subset(models: dict[str, Any], provider: str, binding: str) -> None:
    """The provider's declared vocabularies stay inside the provider-wide fallback."""
    binding_levels = model_catalog().bindings[binding].effort_levels
    assert binding_levels is not None
    fallback = set(binding_levels)
    provider_models = [m for m, info in models.items() if info["provider"] == provider]
    assert provider_models, f"no {provider} models registered for the subset check"
    for model in provider_models:
        options = set(models[model]["reasoning_effort_options"])
        assert options <= fallback, model


def test_get_models_reasoning_effort_options_match_factory_tables() -> None:
    """Gateway's per-model effort option lists come straight off the registry
    (`ModelSpec.effort_levels`), and the registry values for the OpenAI-style
    providers must mirror their plugin binding's clamp vocabularies — a drift
    would silently offer the spawn UI a value build_chat_model then clamps
    away, or hide a value the provider actually accepts."""

    with TestClient(app) as client:
        resp = client.get("/api/models")
    models = resp.json()["models"]

    # The endpoint serves exactly the registry's per-model vocabulary.
    for model, info in models.items():
        expected_levels = model_catalog().models[model].effort_levels
        assert expected_levels is not None, model
        assert info["reasoning_effort_options"] == list(expected_levels), model

    # And the registry vocabulary for the OpenAI-style providers matches the
    # plugin binding's wire clamp, so UI options and clamp cannot diverge.
    # Providers whose whole registry shares one vocabulary mirror the
    # binding vocabulary exactly (UI options == clamp). Gemini
    # diverged deliberately: the agent build path clamps per model
    # (`ModelSpec.effort_levels`), so a model's options equal its own registry
    # vocabulary (verified in the loop above), and the provider-wide table is
    # only the fallback for models without a declared vocabulary (media path).
    # The gemini invariant is that every declared vocabulary stays a subset of
    # that fallback — a model must never accept a level the fallback cannot
    # express.
    _assert_provider_binding_vocabulary(models, "mimo", "mimo-")
    _assert_provider_binding_vocabulary(models, "kimi", "kimi-")
    _assert_provider_binding_vocabulary(models, "glm", "glm-")
    _assert_provider_binding_vocabulary(models, "qwen", "qwen3.8-")
    _assert_provider_vocabulary_subset(models, "gemini", "gemini-")

    assert models["claude-haiku-4-5-20251001"]["reasoning_effort_options"] == ["none", "high"]


def test_get_models_reasoning_effort_default_is_the_per_model_tuning_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every spawnable model publishes a concrete `reasoning_effort_default`
    (what the picker pre-selects), equal to the registry's per-model tuning
    layer — never "" (provider default, not displayable) and never the ladder
    floor by accident. An explicit cluster-wide AVA_REASONING_EFFORT pin must
    NOT leak into the published default: the picker shows the model's own
    default, while the pin is operator policy (visible in the config panel's
    per-model view)."""
    from base.config import settings

    with TestClient(app) as client:
        resp = client.get("/api/models")
    assert resp.status_code == 200
    models = resp.json()["models"]

    # Spot-check the documented vendor defaults (decision doc
    # 2026-07-25-per-model-tuning-values.md Decision 4): deepseek max,
    # claude adaptive family high, gpt medium, kimi/glm max.
    assert models["deepseek-flash"]["reasoning_effort_default"] == "max"
    assert models["claude-sonnet-5"]["reasoning_effort_default"] == "high"
    assert models["claude-haiku-4-5-20251001"]["reasoning_effort_default"] == "none"
    assert models["gpt-5.6-sol"]["reasoning_effort_default"] == "medium"
    assert models["kimi-k3"]["reasoning_effort_default"] == "max"
    assert models["glm-5.2"]["reasoning_effort_default"] == "max"

    # General invariant: default == the registry's tuning value, is concrete,
    # and sits on the model's own ladder (a default off the ladder would be
    # clamped or dropped at build — a UI lie).
    for model, info in models.items():
        expected = model_catalog().models[model].tuning.reasoning_effort
        assert info["reasoning_effort_default"] == expected, model
        assert expected, model  # concrete, never ""
        assert expected in info["reasoning_effort_options"], model

    # The published default is the MODEL's default, not the cluster pin.
    monkeypatch.setattr(settings.lm, "reasoning_effort", "low")
    with TestClient(app) as client:
        resp = client.get("/api/models")
    models = resp.json()["models"]
    assert models["deepseek-flash"]["reasoning_effort_default"] == "max"


class TestSpawn:
    def test_spawn_minimal_no_prompt_no_spawner(self, db_conn: psycopg.Connection) -> None:
        with TestClient(app) as client:
            resp = client.post("/api/agents", json={})
        assert resp.status_code == 201
        new_id = resp.json()["id"]
        row = _agent_row(db_conn, new_id)
        # spawner defaults to 'user' (triggered by UI button)
        assert row == (new_id, "user", None, None, "idling")
        # No inbound delivered
        assert _inbound_rows(db_conn, new_id) == []

    def test_spawn_with_prompt_inserts_chat_inbound(self, db_conn: psycopg.Connection) -> None:
        with TestClient(app) as client:
            resp = client.post("/api/agents", json={"prompt": "\u67e5 X", "prompt_source": "user"})
        new_id = resp.json()["id"]
        assert _inbound_rows(db_conn, new_id) == [("\u67e5 X", "chat", "user")]

    def test_spawn_with_long_prompt_inserts_chat_inbound(self, db_conn: psycopg.Connection) -> None:
        """Long reports are delivered through the shared user-content schema."""
        prompt = "a" * 100_000
        with TestClient(app) as client:
            resp = client.post("/api/agents", json={"prompt": prompt, "prompt_source": "user"})
        assert resp.status_code == 201
        new_id = resp.json()["id"]
        assert _inbound_rows(db_conn, new_id) == [(prompt, "chat", "user")]

    def test_spawn_with_explicit_spawner(self, db_conn: psycopg.Connection) -> None:
        """spawner can be passed explicitly — e.g., when claude-code starts an ava agent, pass 'claude-code'."""
        with TestClient(app) as client:
            resp = client.post("/api/agents", json={"spawner": "claude-code"})
        new_id = resp.json()["id"]
        row = _agent_row(db_conn, new_id)
        assert row is not None and row[1] == "claude-code"

    def test_spawn_settles_withdrawn_model_and_returns_receipt(
        self, db_conn: psycopg.Connection, withdrawn_model: str
    ) -> None:
        """A registered-but-withdrawn llm_model is rewritten to its registered
        fallback before the row is created, and the spawner gets the receipt in
        the response (task #4306) — instead of the withdrawal surfacing only as
        a wake-time normalization log."""
        with TestClient(app) as client:
            resp = client.post("/api/agents", json={"config": {"llm_model": withdrawn_model}})
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["config_normalized"] == {
            "requested": withdrawn_model,
            "resolved": "deepseek-flash",
        }
        with db_conn.cursor() as cur:
            cur.execute("SELECT config_overlay FROM agents_meta WHERE id = %s", (body["id"],))
            assert cur.fetchone() == ({"llm_model": "deepseek-flash"},)

    def test_spawn_available_model_carries_no_receipt(self, db_conn: psycopg.Connection) -> None:
        """An available id is stored as sent — no rewrite, no receipt."""
        with TestClient(app) as client:
            resp = client.post("/api/agents", json={"config": {"llm_model": "deepseek-flash"}})
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body.get("config_normalized") is None
        with db_conn.cursor() as cur:
            cur.execute("SELECT config_overlay FROM agents_meta WHERE id = %s", (body["id"],))
            assert cur.fetchone() == ({"llm_model": "deepseek-flash"},)

    def test_spawn_fork_resolves_latest_and_copies_checkpoint(
        self, db_conn: psycopg.Connection
    ) -> None:
        """fork_from given → gateway internally SELECT max(checkpoint_id) → spawn_agent
        with explicit fork_checkpoint. New agent gets full checkpoint chain."""
        with TestClient(app) as client:
            source = client.post("/api/agents", json={}).json()["id"]
            with db_conn.cursor() as cur:
                for ckpt, parent in [("cka", None), ("ckb", "cka"), ("ckc", "ckb")]:
                    cur.execute(
                        "INSERT INTO checkpoints (thread_id, checkpoint_id, parent_checkpoint_id, "
                        "checkpoint, metadata) VALUES (%s, %s, %s, '{}'::jsonb, '{}'::jsonb)",
                        (str(source), ckpt, parent),
                    )
            db_conn.commit()

            resp = client.post("/api/agents", json={"fork_from": source})
        new_id = resp.json()["id"]
        # agents row records fork_source_*
        row = _agent_row(db_conn, new_id)
        assert row is not None
        assert row[2] == source and row[3] == "ckc"  # latest = ckc
        # new agent gets full a/b/c chain
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
        """fork_from's source has no checkpoint → 409 + reason='fork_source_empty'
        (follows ForkSourceEmpty wire-encoded path, handler maps uniformly, SDK reconstructs from code)."""
        with TestClient(app) as client:
            source = client.post("/api/agents", json={}).json()["id"]
            resp = client.post("/api/agents", json={"fork_from": source})
        assert resp.status_code == 409
        body = resp.json()
        assert body["reason"] == "fork_source_empty"
        assert "no checkpoint" in body["detail"]

    def test_spawn_empty_prompt_validation(
        self,
        db_conn: psycopg.Connection,
    ) -> None:
        """Empty prompt → 422 (pydantic StringConstraints strip + min_length)."""
        with TestClient(app) as client:
            resp = client.post("/api/agents", json={"prompt": "   "})
        assert resp.status_code == 422

    def test_spawn_prompt_without_prompt_source_returns_422(
        self,
        db_conn: psycopg.Connection,
    ) -> None:
        """prompt given but prompt_source missing → 422 (model_validator blocks).
        Prevents the anti-pattern of "caller forgot field silently attributed to user"."""
        with TestClient(app) as client:
            resp = client.post("/api/agents", json={"prompt": "\u67e5 X"})
        assert resp.status_code == 422
        # detail contains validator's Chinese-language prompt
        assert "prompt_source" in resp.text


def test_post_commit_unknown_launch_failure_carries_id_and_cors_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unexpected post-commit error retains the identity and CORS headers."""
    # The autouse conftest fixture stubs forward_spawn_to_remote in-process;
    # this test's monkeypatch runs later and wins, making the route itself blow up.
    import gateway.agents.router as _agents_router
    from ops.rpc_schemas import LaunchAgentRequest, SpawnedAgent

    async def _explode(_db: object, target: str, body: LaunchAgentRequest) -> SpawnedAgent:
        raise RuntimeError("boom")

    monkeypatch.setattr(_agents_router, "forward_spawn_to_remote", _explode)
    allowed_origin = cors_allowed_origins()[0]
    with TestClient(app) as client:
        resp = client.post(
            "/api/agents",
            json={},
            headers={"Origin": allowed_origin},
        )
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
        with TestClient(app) as client:
            agent_id = client.post("/api/agents", json={}).json()["id"]
            resp = client.post(f"/api/agents/{agent_id}/terminate")
        assert resp.status_code == 200
        assert resp.json() == {"status": "enqueued", "open_tasks": None, "shell_sessions": None}
        assert _inbound_rows(db_conn, agent_id) == [("", "terminate", "user")]

    def test_terminate_already_terminated_is_noop(self, db_conn: psycopg.Connection) -> None:
        with TestClient(app) as client:
            agent_id = client.post("/api/agents", json={}).json()["id"]
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
        assert _inbound_rows(db_conn, agent_id) == []  # not delivered

    def test_terminate_nonexistent_404(
        self,
        db_conn: psycopg.Connection,
    ) -> None:
        with TestClient(app) as client:
            resp = client.post("/api/agents/9999/terminate")
        assert resp.status_code == 404
        assert "does not exist" in resp.json()["detail"]

    def test_terminate_force_commits_fence_and_returns_enqueued(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A force request durably fences the identity before reporting acceptance."""

        with TestClient(app) as client:
            agent_id = client.post("/api/agents", json={}).json()["id"]
            resp = client.post(f"/api/agents/{agent_id}/terminate", json={"force": True})
        assert resp.status_code == 200
        assert resp.json() == {"status": "enqueued", "open_tasks": None, "shell_sessions": None}

        # status should become 'terminated'
        row = _agent_row(db_conn, agent_id)
        assert row is not None and row[4] == "terminated"

        # should have inserted an audit inbound (terminate kind)
        assert _inbound_rows(db_conn, agent_id) == [("", "terminate", "user")]

    def test_terminate_force_with_custom_source(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """force=true passes source parameter through to audit inbound."""

        with TestClient(app) as client:
            agent_id = client.post("/api/agents", json={}).json()["id"]
            resp = client.post(
                f"/api/agents/{agent_id}/terminate",
                json={"force": True, "source": "agent:42"},
            )
        assert resp.json() == {"status": "enqueued", "open_tasks": None, "shell_sessions": None}

        # inbound source should be agent:42, not default user
        rows = _inbound_rows(db_conn, agent_id)
        assert rows == [("", "terminate", "agent:42")]

    def test_terminate_force_already_terminated_returns_already_terminated(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Repeated force records a fresh inbound fence without another status transition."""
        with TestClient(app) as client:
            agent_id = client.post("/api/agents", json={}).json()["id"]
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
        assert _inbound_rows(db_conn, agent_id) == [("", "terminate", "user")]

    def test_terminate_force_nonexistent_404(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """force=true for nonexistent agent still 404."""

        with TestClient(app) as client:
            resp = client.post("/api/agents/9999/terminate", json={"force": True})
        assert resp.status_code == 404
        assert "does not exist" in resp.json()["detail"]


class TestAutoResurrect:
    def test_chat_to_terminated_agent_triggers_auto_resurrect(
        self, db_conn: psycopg.Connection
    ) -> None:
        """Sending chat message to a terminated agent → auto-resurrect triggers automatically,
        INSERT 'resurrect' lifecycle inbound."""
        with TestClient(app) as client:
            agent_id = client.post("/api/agents", json={}).json()["id"]
            _terminate_hosted(db_conn, agent_id)
            resp = client.post(
                f"/api/agents/{agent_id}/messages",
                json={"content": "resume your work", "source": "user"},
            )
        assert resp.status_code == 201
        rows = _inbound_rows(db_conn, agent_id)
        # Auto-resurrect inserts a 'resurrect' inbound (source='system') before the chat
        assert ("", "resurrect", "system") in rows
        assert ("resume your work", "chat", "user") in rows

    def test_chat_to_alive_agent_no_resurrect(self, db_conn: psycopg.Connection) -> None:
        """Sending chat to an alive agent → no resurrect inbound inserted."""
        with TestClient(app) as client:
            agent_id = client.post("/api/agents", json={}).json()["id"]
            resp = client.post(
                f"/api/agents/{agent_id}/messages",
                json={"content": "hello", "source": "user"},
            )
        assert resp.status_code == 201
        # Only the chat, no resurrect marker
        rows = _inbound_rows(db_conn, agent_id)
        assert ("hello", "chat", "user") in rows
        # No resurrect row
        resurrect_rows = [r for r in rows if r[1] == "resurrect"]
        assert len(resurrect_rows) == 0

    def test_chat_illegal_source_rejected_422(self, db_conn: psycopg.Connection) -> None:
        """source not in envelope allowlist → 422."""
        with TestClient(app) as client:
            agent_id = client.post("/api/agents", json={}).json()["id"]
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
    """Call a guarded endpoint with the one valid POST body when needed."""
    params = {"caller": caller} if caller is not None else None
    if method == "POST":
        return client.post(path, params=params, json={"query": "test", "k": 1})
    return client.get(path, params=params)


def _stub_result_read_backends(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make non-blocked artifact reads deterministic without external services."""
    import gateway.events.agent_events as agent_events_router
    import gateway.routers.memory as memory_router
    from services.derived.memory_indexer.embeddings import factory as _embedding_factory

    class _StubProvider:
        dim = 8
        fingerprint = "fake:provider:dim=8"

        @staticmethod
        async def embed_query_async(_query: str) -> list[float]:
            return [0.0] * 8

    async def _topk(
        _vector: object, _k: int, _deadline: float, *args: object, **kwargs: object
    ) -> list[str]:
        return []

    async def _stream(*_args: object, **_kwargs: object):
        if False:
            yield ""

    monkeypatch.setattr(_embedding_factory, "get_provider", _StubProvider)
    monkeypatch.setattr(memory_router, "_backend_topk", _topk)
    monkeypatch.setattr(agent_events_router, "event_stream", _stream)


@pytest.mark.parametrize(("method", "path_template"), _RESULT_READ_ENDPOINTS)
def test_eval_isolated_callers_cannot_read_result_surfaces(
    db_conn: psycopg.Connection, method: str, path_template: str
) -> None:
    """Every artifact-read endpoint blocks the SDK-bypassing eval caller."""
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

    with TestClient(app) as client:
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
    """The guard leaves ordinary reads intact and preserves last-message validation."""
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

    with TestClient(app) as client:
        ordinary = _result_read(client, method, path, caller=f"agent:{caller_id}")
        unmarked = _result_read(client, method, path)

    assert ordinary.status_code == 200
    assert unmarked.status_code == (422 if path_template.endswith("/last-message") else 200)
