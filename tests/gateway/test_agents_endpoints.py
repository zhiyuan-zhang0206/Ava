"""Gateway lifecycle endpoints HTTP integration tests.

POST /api/agents (spawn + optional prompt + optional fork_from)
POST /api/agents/{id}/terminate (INSERT terminate inbound)
GET /api/agents (full snapshot of agents_meta table)

`_launch_agent_process` monkeypatch (does not actually start process); uses ava_test DB with real SQL.
"""

from __future__ import annotations

import json
import os
import signal
import threading
from typing import cast

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg_pool import ConnectionPool

from gateway._cors import cors_allowed_origins
from gateway.app import app


def _stub_native_kill(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the native supervisor's kill so force-terminate does not touch a real
    agent process session in the test (there is none)."""

    class _FakeSupervisor:
        @staticmethod
        def kill_session(*_a, **_kw):
            return (True, "noop")

    monkeypatch.setattr("ops.ops_exit.native_proc", lambda: _FakeSupervisor)


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


def test_get_models_returns_grouped_supported_models() -> None:
    with TestClient(app) as client:
        resp = client.get("/api/models")
    assert resp.status_code == 200
    body = resp.json()
    flat = [m for group in body["providers"].values() for m in group]
    assert "deepseek-v4-pro" in flat
    assert "gpt-5.6-sol" in flat
    # additional verified-live models
    assert "claude-sonnet-5" in flat
    assert "gpt-5.6-terra" in flat
    assert "gpt-5.6-luna" in flat
    assert body["models"]["gpt-5.6-sol"]["pricing"] == {
        "input": 5.0,
        "cache_read": 0.5,
        "output": 30.0,
    }
    from shared.config import settings

    assert body["default"] == settings.lm.llm_model


def test_get_models_surfaces_superseded_by(monkeypatch: pytest.MonkeyPatch) -> None:
    """The picker's hide-by-default rule is data, not gateway logic: the
    endpoint publishes each model's ``superseded_by`` straight off the registry,
    and an un-superseded model carries null."""
    from dataclasses import replace

    from shared.lm.registry import MODELS

    monkeypatch.setitem(MODELS, "glm-5.2", replace(MODELS["glm-5.2"], superseded_by="kimi-k3"))
    with TestClient(app) as client:
        resp = client.get("/api/models")
    body = resp.json()
    assert body["models"]["glm-5.2"]["superseded_by"] == "kimi-k3"
    assert body["models"]["deepseek-v4-pro"]["superseded_by"] is None


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


def test_get_models_reasoning_effort_options_match_factory_tables() -> None:
    """Gateway's per-model effort option lists come straight off the registry
    (`ModelSpec.effort_levels`), and the registry values for the OpenAI-style
    providers must mirror their plugin binding's clamp vocabularies — a drift
    would silently offer the spawn UI a value build_chat_model then clamps
    away, or hide a value the provider actually accepts."""
    from shared.lm import provider_api
    from shared.lm.registry import MODELS

    with TestClient(app) as client:
        resp = client.get("/api/models")
    models = resp.json()["models"]

    # The endpoint serves exactly the registry's per-model vocabulary.
    for model, info in models.items():
        expected_levels = MODELS[model].effort_levels
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
    mimo_binding_levels = provider_api.REGISTRY.bindings["mimo-"].effort_levels
    assert mimo_binding_levels is not None
    mimo_models = [m for m, info in models.items() if info["provider"] == "mimo"]
    assert mimo_models, "no mimo models registered for the binding check"
    for model in mimo_models:
        assert models[model]["reasoning_effort_options"] == list(mimo_binding_levels), model
    kimi_binding_levels = provider_api.REGISTRY.bindings["kimi-"].effort_levels
    assert kimi_binding_levels is not None
    kimi_models = [m for m, info in models.items() if info["provider"] == "kimi"]
    assert kimi_models, "no kimi models registered for the binding check"
    for model in kimi_models:
        assert models[model]["reasoning_effort_options"] == list(kimi_binding_levels), model
    glm_binding_levels = provider_api.REGISTRY.bindings["glm-"].effort_levels
    assert glm_binding_levels is not None
    glm_models = [m for m, info in models.items() if info["provider"] == "glm"]
    assert glm_models, "no glm models registered for the binding check"
    for model in glm_models:
        assert models[model]["reasoning_effort_options"] == list(glm_binding_levels), model
    qwen_binding_levels = provider_api.REGISTRY.bindings["qwen3.8-"].effort_levels
    assert qwen_binding_levels is not None
    qwen_models = [m for m, info in models.items() if info["provider"] == "qwen"]
    assert qwen_models, "no qwen models registered for the binding check"
    for model in qwen_models:
        assert models[model]["reasoning_effort_options"] == list(qwen_binding_levels), model
    gemini_binding_levels = provider_api.REGISTRY.bindings["gemini-"].effort_levels
    assert gemini_binding_levels is not None
    gemini_fallback = set(gemini_binding_levels)
    gemini_models = [m for m, info in models.items() if info["provider"] == "gemini"]
    assert gemini_models, "no gemini models registered for the subset check"
    for model in gemini_models:
        options = set(models[model]["reasoning_effort_options"])
        assert options <= gemini_fallback, model

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
    from shared.config import settings
    from shared.lm.registry import MODELS

    with TestClient(app) as client:
        resp = client.get("/api/models")
    assert resp.status_code == 200
    models = resp.json()["models"]

    # Spot-check the documented vendor defaults (decision doc
    # 2026-07-25-per-model-tuning-values.md Decision 4): deepseek max,
    # claude adaptive family high, gpt medium, kimi/glm max.
    assert models["deepseek-v4-pro"]["reasoning_effort_default"] == "max"
    assert models["claude-sonnet-5"]["reasoning_effort_default"] == "high"
    assert models["claude-haiku-4-5-20251001"]["reasoning_effort_default"] == "none"
    assert models["gpt-5.6-sol"]["reasoning_effort_default"] == "medium"
    assert models["kimi-k3"]["reasoning_effort_default"] == "max"
    assert models["glm-5.2"]["reasoning_effort_default"] == "max"

    # General invariant: default == the registry's tuning value, is concrete,
    # and sits on the model's own ladder (a default off the ladder would be
    # clamped or dropped at build — a UI lie).
    for model, info in models.items():
        expected = MODELS[model].tuning.reasoning_effort
        assert info["reasoning_effort_default"] == expected, model
        assert expected, model  # concrete, never ""
        assert expected in info["reasoning_effort_options"], model

    # The published default is the MODEL's default, not the cluster pin.
    monkeypatch.setattr(settings.lm, "reasoning_effort", "low")
    with TestClient(app) as client:
        resp = client.get("/api/models")
    models = resp.json()["models"]
    assert models["deepseek-v4-pro"]["reasoning_effort_default"] == "max"


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


def test_unhandled_route_exception_500_carries_cors_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unhandled route exception surfaces as a 500 WITH CORS headers — the
    catch-all Exception handler routes it back through the middleware stack
    (CORSMiddleware is outermost), so a browser caller sees the real status
    instead of "Failed to fetch" (#187)."""
    # The autouse conftest fixture stubs _forward_spawn_to_remote in-process;
    # this test's monkeypatch runs later and wins, making the route itself blow up.
    import gateway.routers.agents as _agents_router
    from ops.rpc_schemas import LaunchAgentRequest, SpawnedAgent

    async def _explode(target: str, body: LaunchAgentRequest) -> SpawnedAgent:
        raise RuntimeError("boom")

    monkeypatch.setattr(_agents_router, "_forward_spawn_to_remote", _explode)
    # ServerErrorMiddleware re-raises the exception after answering; the test
    # client would otherwise surface it as a test failure instead of the 500.
    allowed_origin = cors_allowed_origins()[0]
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post(
            "/api/agents",
            json={},
            headers={"Origin": allowed_origin},
        )
    assert resp.status_code == 500
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
        assert resp.json() == {"status": "enqueued"}
        assert _inbound_rows(db_conn, agent_id) == [("", "terminate", "user")]

    def test_terminate_foreign_pid_force_marks_without_killing(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A recycled live pid belongs to somebody else, so graceful terminate
        reconciles only the stale row and never touches that process or session."""
        from ops.agent_identity import AgentProcessIdentity

        session_kills: list[tuple[str, bool]] = []
        pid_kills: list[int] = []
        published_agent_ids: list[int] = []

        def _capture_agent_updated(_conn: psycopg.Connection, published_agent_id: int) -> None:
            published_agent_ids.append(published_agent_id)

        class _RecordingSupervisor:
            @staticmethod
            def kill_session(name: str, *, graceful: bool) -> tuple[bool, str]:
                session_kills.append((name, graceful))
                return True, "noop"

        monkeypatch.setattr("ops.ops_exit.native_proc", lambda: _RecordingSupervisor)
        monkeypatch.setattr("ops.ops_exit.force_kill", pid_kills.append)
        monkeypatch.setattr(
            "ops.ops_lifecycle.publish_agent_updated_sync",
            _capture_agent_updated,
        )

        def _foreign_process(_pid: int, _agent_id: int) -> AgentProcessIdentity:
            return AgentProcessIdentity.FOREIGN

        monkeypatch.setattr(
            "ops.ops_lifecycle.probe_agent_process",
            _foreign_process,
        )

        with TestClient(app) as client:
            agent_id = client.post("/api/agents", json={}).json()["id"]
            with db_conn.cursor() as cur:
                cur.execute(
                    "UPDATE agents_meta SET pid = %s WHERE id = %s", (os.getpid(), agent_id)
                )
            db_conn.commit()
            resp = client.post(f"/api/agents/{agent_id}/terminate")

        assert resp.json() == {"status": "already_terminated"}
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT status, termination_source FROM agents_meta WHERE id = %s", (agent_id,)
            )
            assert cur.fetchone() == ("terminated", "user")
        assert _inbound_rows(db_conn, agent_id) == [("", "terminate", "user")]
        assert session_kills == []
        assert pid_kills == []
        assert published_agent_ids == [agent_id]

    def test_terminate_gone_pid_force_marks_without_killing(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A dead pid is a stale row, not an excuse to clear a possibly reused
        session name while reconciling the requested termination."""
        from ops.agent_identity import AgentProcessIdentity

        session_kills: list[tuple[str, bool]] = []
        pid_kills: list[int] = []

        class _RecordingSupervisor:
            @staticmethod
            def kill_session(name: str, *, graceful: bool) -> tuple[bool, str]:
                session_kills.append((name, graceful))
                return True, "noop"

        monkeypatch.setattr("ops.ops_exit.native_proc", lambda: _RecordingSupervisor)
        monkeypatch.setattr("ops.ops_exit.force_kill", pid_kills.append)

        def _gone_process(_pid: int, _agent_id: int) -> AgentProcessIdentity:
            return AgentProcessIdentity.GONE

        monkeypatch.setattr(
            "ops.ops_lifecycle.probe_agent_process",
            _gone_process,
        )

        with TestClient(app) as client:
            agent_id = client.post("/api/agents", json={}).json()["id"]
            with db_conn.cursor() as cur:
                cur.execute(
                    "UPDATE agents_meta SET pid = %s WHERE id = %s", (2_147_483_647, agent_id)
                )
            db_conn.commit()
            resp = client.post(f"/api/agents/{agent_id}/terminate")

        assert resp.json() == {"status": "already_terminated"}
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT status, termination_source FROM agents_meta WHERE id = %s", (agent_id,)
            )
            assert cur.fetchone() == ("terminated", "user")
        assert _inbound_rows(db_conn, agent_id) == [("", "terminate", "user")]
        assert session_kills == []
        assert pid_kills == []

    def test_terminate_owned_pid_enqueues_even_when_liveness_probe_is_inconclusive(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Positive identity evidence keeps graceful termination on the inbound
        path even if the obsolete liveness check would report the pid dead."""
        from ops.agent_identity import AgentProcessIdentity

        _stub_native_kill(monkeypatch)

        def _no_kill(_pid: int) -> None:
            return None

        def _not_alive(_pid: int) -> bool:
            return False

        def _owned_process(_pid: int, _agent_id: int) -> AgentProcessIdentity:
            return AgentProcessIdentity.OWNED

        monkeypatch.setattr("ops.ops_exit.force_kill", _no_kill)
        monkeypatch.setattr("ops.agent_identity.process_alive", _not_alive, raising=False)
        monkeypatch.setattr(
            "ops.ops_lifecycle.probe_agent_process",
            _owned_process,
        )

        with TestClient(app) as client:
            agent_id = client.post("/api/agents", json={}).json()["id"]
            with db_conn.cursor() as cur:
                cur.execute(
                    "UPDATE agents_meta SET pid = %s WHERE id = %s", (os.getpid(), agent_id)
                )
            db_conn.commit()
            resp = client.post(f"/api/agents/{agent_id}/terminate")

        assert resp.json() == {"status": "enqueued"}
        with db_conn.cursor() as cur:
            cur.execute("SELECT status FROM agents_meta WHERE id = %s", (agent_id,))
            assert cur.fetchone() == ("idling",)
        assert _inbound_rows(db_conn, agent_id) == [("", "terminate", "user")]

    def test_terminate_unreadable_pid_enqueues_as_resident(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A pid with unreadable argv remains resident: lack of identity evidence
        must not turn a graceful request into an immediate termination."""
        from ops.agent_identity import AgentProcessIdentity

        _stub_native_kill(monkeypatch)

        def _no_kill(_pid: int) -> None:
            return None

        def _not_alive(_pid: int) -> bool:
            return False

        def _unreadable_process(_pid: int, _agent_id: int) -> AgentProcessIdentity:
            return AgentProcessIdentity.UNREADABLE

        monkeypatch.setattr("ops.ops_exit.force_kill", _no_kill)
        monkeypatch.setattr("ops.agent_identity.process_alive", _not_alive, raising=False)
        monkeypatch.setattr(
            "ops.ops_lifecycle.probe_agent_process",
            _unreadable_process,
        )

        with TestClient(app) as client:
            agent_id = client.post("/api/agents", json={}).json()["id"]
            with db_conn.cursor() as cur:
                cur.execute(
                    "UPDATE agents_meta SET pid = %s WHERE id = %s", (os.getpid(), agent_id)
                )
            db_conn.commit()
            resp = client.post(f"/api/agents/{agent_id}/terminate")

        assert resp.json() == {"status": "enqueued"}
        with db_conn.cursor() as cur:
            cur.execute("SELECT status FROM agents_meta WHERE id = %s", (agent_id,))
            assert cur.fetchone() == ("idling",)
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
        assert resp.json() == {"status": "already_terminated"}
        assert _inbound_rows(db_conn, agent_id) == []  # not delivered

    def test_terminate_nonexistent_404(
        self,
        db_conn: psycopg.Connection,
    ) -> None:
        with TestClient(app) as client:
            resp = client.post("/api/agents/9999/terminate")
        assert resp.status_code == 404
        assert "does not exist" in resp.json()["detail"]

    def test_terminate_force_kills_and_returns_force_killed(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """force=true skips inbound path, directly kills the session + forcefully marks
        terminated + inserts audit inbound."""
        # stub kill-session — no real session in test
        _stub_native_kill(monkeypatch)

        with TestClient(app) as client:
            agent_id = client.post("/api/agents", json={}).json()["id"]
            resp = client.post(f"/api/agents/{agent_id}/terminate", json={"force": True})
        assert resp.status_code == 200
        assert resp.json() == {"status": "force_killed"}

        # status should become 'terminated'
        row = _agent_row(db_conn, agent_id)
        assert row is not None and row[4] == "terminated"

        # should have inserted an audit inbound (terminate kind)
        assert _inbound_rows(db_conn, agent_id) == [("", "terminate", "user")]

    def test_terminate_force_with_custom_source(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """force=true passes source parameter through to audit inbound."""
        _stub_native_kill(monkeypatch)

        with TestClient(app) as client:
            agent_id = client.post("/api/agents", json={}).json()["id"]
            resp = client.post(
                f"/api/agents/{agent_id}/terminate",
                json={"force": True, "source": "agent:42"},
            )
        assert resp.json() == {"status": "force_killed"}

        # inbound source should be agent:42, not default user
        rows = _inbound_rows(db_conn, agent_id)
        assert rows == [("", "terminate", "agent:42")]

    def test_terminate_force_already_terminated_returns_already_terminated(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Repeated force preserves its wire response but records a fresh
        fence and clears the exact session without killing a possibly-reused
        stale PID or faking a new status transition."""
        session_kills: list[tuple[str, bool]] = []
        pid_kills: list[int] = []

        class _RecordingSupervisor:
            @staticmethod
            def kill_session(name: str, *, graceful: bool):
                session_kills.append((name, graceful))
                return (True, "noop")

        monkeypatch.setattr("ops.ops_exit.native_proc", lambda: _RecordingSupervisor)
        monkeypatch.setattr("ops.ops_exit.force_kill", pid_kills.append)

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
        assert resp.json() == {"status": "already_terminated"}
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
        assert len(session_kills) == 1
        assert session_kills[0][1] is False
        assert pid_kills == []

    def test_terminate_force_nonexistent_404(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """force=true for nonexistent agent still 404."""
        _stub_native_kill(monkeypatch)

        with TestClient(app) as client:
            resp = client.post("/api/agents/9999/terminate", json={"force": True})
        assert resp.status_code == 404
        assert "does not exist" in resp.json()["detail"]

    def test_terminate_force_with_pid_sends_sigkill(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """force=true and pid is not NULL, sends SIGKILL."""
        _stub_native_kill(monkeypatch)
        from ops.agent_identity import AgentProcessIdentity

        def _owned_process(_pid: int, _agent_id: int) -> AgentProcessIdentity:
            return AgentProcessIdentity.OWNED

        monkeypatch.setattr(
            "ops.ops_exit.probe_agent_process",
            _owned_process,
        )

        # track os.kill calls
        kill_calls: list[tuple] = []
        monkeypatch.setattr("shared.proc.os.kill", lambda pid, sig: kill_calls.append((pid, sig)))  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]

        with TestClient(app) as client:
            agent_id = client.post("/api/agents", json={}).json()["id"]
            # manually set pid — simulating agent running
            with db_conn.cursor() as cur:
                cur.execute("UPDATE agents_meta SET pid = 12345 WHERE id = %s", (agent_id,))
            db_conn.commit()
            resp = client.post(f"/api/agents/{agent_id}/terminate", json={"force": True})
        assert resp.json() == {"status": "force_killed"}
        assert kill_calls == [(12345, signal.SIGKILL)]


class TestAutoResurrect:
    def test_chat_to_terminated_agent_triggers_auto_resurrect(
        self, db_conn: psycopg.Connection
    ) -> None:
        """Sending chat message to a terminated agent → auto-resurrect triggers automatically,
        INSERT 'resurrect' lifecycle inbound."""
        with TestClient(app) as client:
            agent_id = client.post("/api/agents", json={}).json()["id"]
            with db_conn.cursor() as cur:
                cur.execute(
                    "UPDATE agents_meta SET status = 'terminated' WHERE id = %s", (agent_id,)
                )
            db_conn.commit()
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


class TestSystemNote:
    def test_system_note_inserts_system_note_inbound(self, db_conn: psycopg.Connection) -> None:
        """POST /system-note → kind='system_note' inbound with the task note tag
        (no peer-chat row), delivered to a live agent without resurrection."""
        with TestClient(app) as client:
            agent_id = client.post("/api/agents", json={}).json()["id"]
            resp = client.post(
                f"/api/agents/{agent_id}/system-note",
                json={"content": 'Task #1 "t" is now assigned to you.'},
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
        assert payload == {"note_tag": "task"}
        # No resurrect row for a live agent.
        assert all(r[1] != "resurrect" for r in rows)

    def test_system_note_to_terminated_agent_resurrects_when_requested(
        self, db_conn: psycopg.Connection
    ) -> None:
        """resurrect=True (task assignment) revives a terminated target, like chat."""
        with TestClient(app) as client:
            agent_id = client.post("/api/agents", json={}).json()["id"]
            with db_conn.cursor() as cur:
                cur.execute(
                    "UPDATE agents_meta SET status = 'terminated' WHERE id = %s", (agent_id,)
                )
            db_conn.commit()
            resp = client.post(
                f"/api/agents/{agent_id}/system-note",
                json={"content": 'Task #1 "t" is now assigned to you.'},
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
            with db_conn.cursor() as cur:
                cur.execute(
                    "UPDATE agents_meta SET status = 'terminated' WHERE id = %s", (agent_id,)
                )
            db_conn.commit()
            resp = client.post(
                f"/api/agents/{agent_id}/system-note",
                json={"content": 'Task #1 "t" was updated.', "resurrect": False},
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
            )
        assert resp.status_code == 422

    def test_system_note_task_id_requires_task_tag(self, db_conn: psycopg.Connection) -> None:
        """Task attribution cannot silently ride an unrelated system-note kind."""
        with TestClient(app) as client:
            agent_id = client.post("/api/agents", json={}).json()["id"]
            resp = client.post(
                f"/api/agents/{agent_id}/system-note",
                json={"content": "x", "note_tag": "heartbeat", "task_id": 42},
            )
        assert resp.status_code == 422
        assert "task_id requires note_tag='task'" in str(resp.json())

    def test_system_note_task_id_must_name_an_existing_task(
        self, db_conn: psycopg.Connection
    ) -> None:
        """A nonexistent task must not create an LLM usage event with no total."""
        with TestClient(app) as client:
            agent_id = client.post("/api/agents", json={}).json()["id"]
            resp = client.post(
                f"/api/agents/{agent_id}/system-note",
                json={"content": "x", "task_id": 999_999},
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
            )
        assert resp.status_code == 422
        assert "is not owned by agent" in str(resp.json())

    def test_system_note_task_ownership_stays_locked_through_enqueue(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A reassignment cannot land after validation but before task-note enqueueing."""
        from gateway.routers import agents_state
        from shared.db import connect, pool

        enqueue_entered, release_enqueue, reassign_started, reassign_finished = (
            threading.Event(),
            threading.Event(),
            threading.Event(),
            threading.Event(),
        )
        errors: list[Exception] = []

        def pause_enqueue(
            db: psycopg.Connection,
            agent_id: int,
            content: str,
            source: str,
            kind: str = "chat",
            payload: dict[str, object] | None = None,
        ) -> int:
            del db, agent_id, content, source, kind, payload
            enqueue_entered.set()
            assert release_enqueue.wait(timeout=2)
            return 1

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
        monkeypatch.setattr(agents_state, "insert_inbound_message", pause_enqueue)

        def enqueue(note_pool: ConnectionPool) -> None:
            try:
                agents_state._system_note_blocking(
                    note_pool,
                    owner_id,
                    "x",
                    "system",
                    "task",
                    task_id,
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
            )
        assert resp.status_code == 422


class TestRestart:
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

    def test_restart_nonexistent_404(
        self,
        db_conn: psycopg.Connection,
    ) -> None:
        with TestClient(app) as client:
            resp = client.post("/api/agents/9999/restart")
        assert resp.status_code == 404


class TestList:
    def test_get_agents_returns_all_with_status_and_lineage(
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
        rows = resp.json()
        assert len(rows) == 2
        assert set(rows[0]) == {
            "agent_id",
            "spawner",
            "fork_source_agent_id",
            "fork_source_checkpoint_id",
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
            "last_probe_at",
            "notices_awaiting_response",
            "unread_notice_count",
            "heartbeat_paused_until",
            "observation",
        }
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
        """The default stays historical; frontend scopes partition the roster."""
        with TestClient(app) as client:
            live_id = client.post("/api/agents", json={}).json()["id"]
            terminated_id = client.post("/api/agents", json={}).json()["id"]
            with db_conn.cursor() as cur:
                cur.execute(
                    "UPDATE agents_meta SET status = 'terminated' WHERE id = %s",
                    (terminated_id,),
                )
            db_conn.commit()

            all_rows = client.get("/api/agents").json()
            live_rows = client.get("/api/agents", params={"scope": "live"}).json()
            terminated_rows = client.get("/api/agents", params={"scope": "terminated"}).json()

        assert {row["agent_id"] for row in all_rows} == {live_id, terminated_id}
        assert [row["agent_id"] for row in live_rows] == [live_id]
        assert [row["agent_id"] for row in terminated_rows] == [terminated_id]

    def test_get_agents_rejects_unknown_scope(self) -> None:
        with TestClient(app) as client:
            response = client.get("/api/agents", params={"scope": "future"})
        assert response.status_code == 422

    def test_get_agents_summary_omits_detail_only_fields(self, db_conn: psycopg.Connection) -> None:
        """List consumers receive only the fields they actually render or parse."""
        with TestClient(app) as client:
            agent_id = client.post("/api/agents", json={}).json()["id"]
            response = client.get("/api/agents", params={"fields": "summary"})
            detail = client.get(f"/api/agents/{agent_id}")

        assert response.status_code == 200
        row = response.json()[0]
        assert set(row) == {
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
            "notices_awaiting_response",
            "unread_notice_count",
            "heartbeat_paused_until",
            "observation",
        }
        assert row["observation"]["runtime_owner"] == "unknown"
        assert row["observation"]["machine_probe_at"] is None
        assert row["observation"]["machine_probe_valid_until"] is None
        assert detail.status_code == 200
        assert "fork_source_checkpoint_id" in detail.json()
        assert "last_probe_at" in detail.json()

    def test_get_agents_compact_has_only_cli_columns(self, db_conn: psycopg.Connection) -> None:
        """The CLI projection contains exactly the three columns it renders."""
        with TestClient(app) as client:
            agent_id = client.post("/api/agents", json={}).json()["id"]
            client.patch(f"/api/agents/{agent_id}", json={"label": "alpha"})
            response = client.get("/api/agents", params={"fields": "compact"})

        assert response.status_code == 200
        assert response.json() == [{"agent_id": agent_id, "status": "idling", "label": "alpha"}]

    def test_get_agents_rejects_unknown_fields_projection(self) -> None:
        with TestClient(app) as client:
            response = client.get("/api/agents", params={"fields": "minimal"})
        assert response.status_code == 422

    def test_get_agents_joins_thread_label(
        self,
        db_conn: psycopg.Connection,
    ) -> None:
        """label field fetched from agents JOIN — after PATCH write, GET should see it."""
        with TestClient(app) as client:
            a_id = client.post("/api/agents", json={}).json()["id"]
            client.patch(f"/api/agents/{a_id}", json={"label": "\u6211\u7684 agent"})
            resp = client.get("/api/agents")
        assert resp.status_code == 200
        by_id = {r["agent_id"]: r for r in resp.json()}
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
        list_row = next(r for r in list_resp.json() if r["agent_id"] == a_id)
        assert list_row["machine"] == "test-host"
        assert single_resp.json()["machine"] == "test-host"

    def test_get_agents_empty_returns_empty_list(
        self,
        db_conn: psycopg.Connection,
    ) -> None:
        with TestClient(app) as client:
            resp = client.get("/api/agents")
        assert resp.status_code == 200
        assert resp.json() == []


class TestGetLastMessage:
    def test_any_agent_can_query_unrelated_agent(self, db_conn: psycopg.Connection) -> None:
        """Any agent in the cluster can query — not just spawn-chain ancestors."""
        from langchain_core.messages import AIMessage
        from langgraph.checkpoint.base import empty_checkpoint
        from langgraph.checkpoint.postgres import PostgresSaver

        from shared.config import settings
        from shared.db import create_agent

        # Create two unrelated agents (no spawn chain).
        # create_agent inserts into agents (LangGraph thread); we also need
        # agents_meta for the endpoint's existence check.
        agent_a = create_agent(db_conn)
        agent_b = create_agent(db_conn)
        with db_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO agents_meta (id, spawner, status) VALUES (%s, 'test', 'running')",
                (agent_a,),
            )
            cur.execute(
                "INSERT INTO agents_meta (id, spawner, status) VALUES (%s, 'test', 'running')",
                (agent_b,),
            )
        db_conn.commit()

        # Write a checkpoint for agent_a with an AIMessage
        msg = AIMessage(content="hello from agent A", id="msg-1")
        ckpt = empty_checkpoint()
        ckpt["channel_values"] = {"messages": [msg]}
        ckpt["channel_versions"] = {"messages": "1", "__start__": "1"}
        with PostgresSaver.from_conn_string(settings.data_plane.db_url) as saver:
            saver.setup()
            saver.put(
                config={"configurable": {"thread_id": str(agent_a), "checkpoint_ns": ""}},
                checkpoint=ckpt,
                metadata={"source": "input", "step": 1, "parents": {}},
                new_versions={"messages": "1"},
            )

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
        from shared.db import create_agent

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
        from shared.db import create_agent

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
        from shared.db import create_agent

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
        from shared.db import create_agent

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

    def test_falls_back_to_checkpoint_when_column_null(self, db_conn: psycopg.Connection) -> None:
        """Backward compat: when last_message_text IS NULL, scan checkpoint."""
        from langchain_core.messages import AIMessage
        from langgraph.checkpoint.base import empty_checkpoint
        from langgraph.checkpoint.postgres import PostgresSaver

        from shared.config import settings
        from shared.db import create_agent

        agent_id = create_agent(db_conn)
        with db_conn.cursor() as cur:
            # last_message_text is left NULL (default)
            cur.execute(
                "INSERT INTO agents_meta (id, spawner, status) VALUES (%s, 'test', 'running')",
                (agent_id,),
            )
        db_conn.commit()

        # Write a checkpoint with an AIMessage
        msg = AIMessage(content="from checkpoint", id="msg-1")
        ckpt = empty_checkpoint()
        ckpt["channel_values"] = {"messages": [msg]}
        ckpt["channel_versions"] = {"messages": "1", "__start__": "1"}
        with PostgresSaver.from_conn_string(settings.data_plane.db_url) as saver:
            saver.setup()
            saver.put(
                config={"configurable": {"thread_id": str(agent_id), "checkpoint_ns": ""}},
                checkpoint=ckpt,
                metadata={"source": "input", "step": 1, "parents": {}},
                new_versions={"messages": "1"},
            )

        with TestClient(app) as client:
            resp = client.get(
                f"/api/agents/{agent_id}/last-message",
                params={"caller": "agent:99999"},
            )
        assert resp.status_code == 200
        assert resp.json()["text"] == "from checkpoint"

    def test_falls_back_to_checkpoint_when_column_missing(
        self, db_conn: psycopg.Connection
    ) -> None:
        """When last_message_text column does not exist (migration not applied),
        fall back to checkpoint scan instead of returning 500."""
        from langchain_core.messages import AIMessage
        from langgraph.checkpoint.base import empty_checkpoint
        from langgraph.checkpoint.postgres import PostgresSaver

        from shared.config import settings
        from shared.db import create_agent

        agent_id = create_agent(db_conn)
        with db_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO agents_meta (id, spawner, status) VALUES (%s, 'test', 'running')",
                (agent_id,),
            )
        db_conn.commit()

        # Write a checkpoint with an AIMessage
        msg = AIMessage(content="from checkpoint fallback", id="msg-1")
        ckpt = empty_checkpoint()
        ckpt["channel_values"] = {"messages": [msg]}
        ckpt["channel_versions"] = {"messages": "1", "__start__": "1"}
        with PostgresSaver.from_conn_string(settings.data_plane.db_url) as saver:
            saver.setup()
            saver.put(
                config={"configurable": {"thread_id": str(agent_id), "checkpoint_ns": ""}},
                checkpoint=ckpt,
                metadata={"source": "input", "step": 1, "parents": {}},
                new_versions={"messages": "1"},
            )

        # Drop the last_message_text column to simulate migration not applied
        with db_conn.cursor() as cur:
            cur.execute("ALTER TABLE agents_meta DROP COLUMN IF EXISTS last_message_text")
        db_conn.commit()

        with TestClient(app) as client:
            resp = client.get(
                f"/api/agents/{agent_id}/last-message",
                params={"caller": "agent:99999"},
            )
        assert resp.status_code == 200
        assert resp.json()["text"] == "from checkpoint fallback"

        # Restore the column for subsequent tests
        with db_conn.cursor() as cur:
            cur.execute("ALTER TABLE agents_meta ADD COLUMN IF NOT EXISTS last_message_text TEXT")
        db_conn.commit()

    def test_404_for_nonexistent_agent(self, db_conn: psycopg.Connection) -> None:
        with TestClient(app) as client:
            resp = client.get(
                "/api/agents/99999/last-message",
                params={"caller": "agent:1"},
            )
        assert resp.status_code == 404


_RESULT_READ_ENDPOINTS = [
    ("GET", "/api/agents/{agent_id}/messages"),
    ("GET", "/api/agents/{agent_id}/traces/trace-1/messages"),
    ("GET", "/api/agents/{agent_id}/last-message"),
    ("GET", "/api/agents/{agent_id}/pending"),
    ("GET", "/api/agents/{agent_id}/activity"),
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
    import gateway.routers.agent_events as agent_events_router
    import gateway.routers.memory as memory_router
    from gateway import loki_events
    from services.memory_indexer.embeddings import factory as _embedding_factory

    def _query(**_kwargs: object) -> tuple[list[dict[str, object]], bool]:
        return [], False

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

    monkeypatch.setattr(loki_events, "query_events", _query)
    monkeypatch.setattr(_embedding_factory, "get_provider", _StubProvider)
    monkeypatch.setattr(memory_router, "_backend_topk", _topk)
    monkeypatch.setattr(agent_events_router, "event_stream", _stream)


@pytest.mark.parametrize(("method", "path_template"), _RESULT_READ_ENDPOINTS)
def test_eval_isolated_callers_cannot_read_result_surfaces(
    db_conn: psycopg.Connection, method: str, path_template: str
) -> None:
    """Every artifact-read endpoint blocks the SDK-bypassing eval caller."""
    from shared.db import create_agent

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
    from shared.db import create_agent

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
