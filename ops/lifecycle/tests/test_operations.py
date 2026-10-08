# pyright: reportUnknownArgumentType=warning, reportUnknownLambdaType=warning
"""The `ops` op clusters — ops-server-callable RPC implementations.

Free functions backing both the gateway FastAPI handlers and the in-process dispatch in
services/agent_runner/agent_ops/daemon.py. These tests pin the contract independently of either entry point:
dispatch routing in the ops server has its own coverage in services/agent_runner/agent_ops/tests/test_daemon.py,
endpoint smoke tests live in tests/components/gateway/test_cluster_endpoints.py.
"""

from __future__ import annotations

import re
from unittest.mock import AsyncMock

import psycopg
import pytest
from pydantic import ValidationError

from base.agents import ResurrectResult, TerminateResult
from base.agents.messages.inbound import InboundKind
from base.db import Database
from base.deploy.maintenance.tests.test_admission import isolate as isolate
from base.events.live.bus import EventBus
from ops import lifecycle
from ops.lifecycle import launch
from ops.rpc_schemas import (
    RestartAgentRequest,
    ResurrectAgentRequest,
    ResurrectAgentResponse,
    SpawnAgentRequest,
    TerminateAgentRequest,
)


def _db() -> Database:
    return Database.from_settings()


class TestSpawnAgentRequestSourceValidation:
    """F1: an illegal prompt_source must be rejected at the schema boundary,
    not silently accepted and deferred to the agent claim node — where an
    unrecognized envelope source raises ValueError and kills the just-spawned
    process. The schema reuses base.agents.messages.envelope.validate_source so the legal set
    stays single-sourced with the claim-side wrap."""

    def test_rejects_unrecognized_source(self) -> None:
        # The old `ui:` channel prefix is gone — it must now fail the boundary.
        with pytest.raises(ValidationError):
            SpawnAgentRequest(prompt="hi", prompt_source="ui:web")

    def test_accepts_user_source(self) -> None:
        body = SpawnAgentRequest(prompt="hi", prompt_source="user")
        assert body.prompt_source == "user"

    def test_accepts_agent_source(self) -> None:
        body = SpawnAgentRequest(prompt="hi", prompt_source="agent:3")
        assert body.prompt_source == "agent:3"

    def test_no_source_validation_without_prompt(self) -> None:
        # prompt_source is only meaningful alongside a prompt; a spawn without a
        # prompt (fork / blank agent) carries no source to validate.
        body = SpawnAgentRequest(spawner="user")
        assert body.prompt_source is None


class TestRestartAgentRequestConfigOverlay:
    """Restart overlays fail at both HTTP and runner schema boundaries."""

    @pytest.mark.parametrize("profile", ["gateway", "runner"])
    def test_accepts_agent_domain_overlay_in_boundary_profiles(
        self, monkeypatch: pytest.MonkeyPatch, profile: str
    ) -> None:
        """Schema validation must not depend on the boundary process's domains."""
        import base.config as base_config
        from base.config import Settings

        monkeypatch.setattr(base_config, "settings", Settings(profile=profile))

        body = RestartAgentRequest(config_overlay={"completion_notice_policy": "hourly"})

        assert body.config_overlay == {"completion_notice_policy": "hourly"}

    @pytest.mark.parametrize("profile", ["gateway", "runner"])
    def test_rejects_invalid_agent_domain_overlay_in_boundary_profiles(
        self, monkeypatch: pytest.MonkeyPatch, profile: str
    ) -> None:
        """Profile-limited schema validation still rejects invalid agent settings."""
        import base.config as base_config
        from base.config import Settings

        monkeypatch.setattr(base_config, "settings", Settings(profile=profile))

        with pytest.raises(ValidationError):
            RestartAgentRequest(config_overlay={"completion_notice_policy": "bogus"})

    def test_validates_sandbox_overlay_in_gateway_profile(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The gateway does not construct sandbox, but must validate its fields."""
        import base.config as base_config
        from base.config import Settings

        monkeypatch.setattr(base_config, "settings", Settings(profile="gateway"))

        body = RestartAgentRequest(config_overlay={"syntax_fix_ruff_format": True})

        assert body.config_overlay == {"syntax_fix_ruff_format": True}
        with pytest.raises(ValidationError):
            RestartAgentRequest(config_overlay={"syntax_fix_ruff_format": "not-a-bool"})

    @pytest.mark.parametrize(
        "config_overlay",
        [
            {"definitely_not_a_config_field": "x"},
            {"heartbeat_pause_max_seconds": "not-a-number"},
            {"reasoning_effort": "turbo"},
        ],
    )
    def test_rejects_invalid_overlay(self, config_overlay: dict[str, object]) -> None:
        with pytest.raises(ValidationError):
            RestartAgentRequest(config_overlay=config_overlay)

    @pytest.mark.parametrize("config_overlay", [None, {}])
    def test_accepts_legacy_empty_overlay_forms(
        self, config_overlay: dict[str, object] | None
    ) -> None:
        body = RestartAgentRequest(config_overlay=config_overlay)
        assert body.config_overlay == config_overlay


@pytest.fixture
def stub_pool() -> object:
    """Sentinel pool — every op call below mocks the gateway/agents helpers so
    the pool is never touched, but the signature still requires an object."""
    return object()


class TestSpawnPrechecksBlocking:
    """The gateway-side prechecks (fork checkpoint resolution) that used to run
    inside spawn_agent_op — now called by create_and_launch_agent before the row
    INSERT."""

    @pytest.fixture(autouse=True)
    def model_preflight(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """These tests isolate checkpoint lookup; DB-backed model checks have their own tests."""
        from base.lm import model_config

        monkeypatch.setattr(
            model_config, "validate_spawn_model_config", lambda *_args: "deepseek-flash"
        )

    class _FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

    class _FakeConn:
        def cursor(self):
            return TestSpawnPrechecksBlocking._FakeCursor()

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

    class _FakePool:
        def connection(self):
            return TestSpawnPrechecksBlocking._FakeConn()

    def test_fork_resolves_checkpoint(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """fork_from -> latest_checkpoint_id resolves to an explicit id, not 'latest'."""
        monkeypatch.setattr(launch, "latest_checkpoint_id", lambda _cur, _aid: "ckpt:v1")
        checkpoint = launch.spawn_prechecks_blocking(
            SpawnAgentRequest(spawner="user", fork_from=3),
            self._FakePool(),  # type: ignore[arg-type]
        )
        assert checkpoint == "ckpt:v1"

    def test_fork_empty_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """fork_from with no checkpoint raises ForkSourceEmpty (wire-mapped to 409)."""
        from base.agents import ForkSourceEmpty

        monkeypatch.setattr(launch, "latest_checkpoint_id", lambda _cur, _aid: None)
        with pytest.raises(ForkSourceEmpty):
            launch.spawn_prechecks_blocking(
                SpawnAgentRequest(spawner="user", fork_from=3),
                self._FakePool(),  # type: ignore[arg-type]
            )

    def test_plain_spawn_no_checkpoint_lookup(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No fork_from -> no checkpoint lookup, returns None."""
        looked_up: list[object] = []

        def _fake_lookup(_cur: object, _aid: object) -> str:
            looked_up.append(1)
            return "never"

        monkeypatch.setattr(launch, "latest_checkpoint_id", _fake_lookup)
        checkpoint = launch.spawn_prechecks_blocking(
            SpawnAgentRequest(spawner="user"),
            self._FakePool(),  # type: ignore[arg-type]
        )
        assert checkpoint is None
        assert looked_up == []


async def test_restart_agent_op_terminated_short_circuits(
    monkeypatch: pytest.MonkeyPatch,
    db_conn: psycopg.Connection,
    database: Database,
    event_bus: EventBus,
) -> None:
    from ops.tests.pool_support import make_test_pool
    from tests.fixtures.units import spawn_agent

    agent_id = spawn_agent()
    db_conn.execute("UPDATE agents_meta SET status='terminated' WHERE id=%s", (agent_id,))
    db_conn.commit()
    wake = AsyncMock()
    monkeypatch.setattr(lifecycle, "publish_inbound_arrived", wake)
    with make_test_pool() as pool:
        resp = await lifecycle.restart_agent_op(
            database, event_bus, agent_id, RestartAgentRequest(source="user"), pool
        )
    assert resp.status == "already_terminated"
    wake.assert_not_awaited()
    assert db_conn.execute(
        "SELECT count(*) FROM inbound_messages WHERE agent_id=%s AND kind='restart'", (agent_id,)
    ).fetchone() == (0,)


@pytest.mark.asyncio
async def test_restart_lifecycle_op_validates_overlay_on_the_runner(
    stub_pool: object,
    database: Database,
    event_bus: EventBus,
) -> None:
    """The runner reparses forwarded restart bodies before any DB write."""
    with pytest.raises(ValidationError):
        await lifecycle.lifecycle_op(
            database,
            event_bus,
            "/api/agents/9/restart",
            {"config_overlay": {"definitely_not_a_config_field": "x"}},
            stub_pool,  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_resurrect_agent_op_alive_returns_already_alive(
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    event_bus: EventBus,
) -> None:
    from base.agents import AgentStatus

    monkeypatch.setattr(lifecycle, "get_agent_status", lambda _db, _aid: AgentStatus.RUNNING)
    resp = await lifecycle.resurrect_agent_op(
        database, event_bus, 9, ResurrectAgentRequest(prompt="test")
    )
    assert resp.status == "already_alive"


@pytest.mark.asyncio
async def test_resurrect_agent_op_stale_trigger_returns_idempotent_noop(
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    event_bus: EventBus,
) -> None:
    """The internal guarded path treats a stale chat as an expected no-launch
    race, while leaving the still-terminated status for its caller to return."""
    from base.agents import AgentStatus
    from ops.agents.wake import ResurrectTriggerStaleError

    def _terminated(_db: object, _agent_id: int) -> AgentStatus:
        return AgentStatus.TERMINATED

    monkeypatch.setattr(lifecycle, "get_agent_status", _terminated)

    def _stale(_db: object, _bus: object, *_args: object, **_kwargs: object) -> None:
        raise ResurrectTriggerStaleError("trigger chat no longer qualifies")

    monkeypatch.setattr(lifecycle, "resurrect_agent", _stale)
    resp = await lifecycle.resurrect_agent_op(
        database,
        event_bus,
        9,
        ResurrectAgentRequest(resurrected_by="system"),
        trigger_inbound_id=123,
        trigger_inbound_kind=InboundKind.CHAT,
    )
    assert resp.status == "already_alive"


@pytest.mark.asyncio
async def test_terminate_agent_op_terminated_short_circuits(
    monkeypatch: pytest.MonkeyPatch, stub_pool: object, database: Database, event_bus: EventBus
) -> None:
    from base.agents import AgentStatus

    monkeypatch.setattr(lifecycle, "get_agent_status", lambda _db, _aid: AgentStatus.TERMINATED)

    def _no_kill(_aid: int) -> list[int]:
        raise AssertionError("a terminate without the option never kills shell sessions")

    monkeypatch.setattr(lifecycle, "kill_agent_shells", _no_kill)
    resp = await lifecycle.terminate_agent_op(
        database,
        event_bus,
        9,
        TerminateAgentRequest(),
        stub_pool,  # type: ignore[arg-type]
    )
    assert resp.status == "already_terminated"
    assert resp.shell_sessions is None


@pytest.mark.asyncio
async def test_lifecycle_op_parses_path_to_terminate(
    monkeypatch: pytest.MonkeyPatch, stub_pool: object, database: Database, event_bus: EventBus
) -> None:

    captured: dict[str, object] = {}

    async def _fake_terminate(_db: object, _bus: object, agent_id, body, pool):  # type: ignore[no-untyped-def]
        captured["agent_id"] = agent_id
        captured["force"] = body.force  # pyright: ignore[reportUnknownMemberType]
        from ops.rpc_schemas import TerminateAgentResponse

        return TerminateAgentResponse(status=TerminateResult.ENQUEUED)

    monkeypatch.setattr(lifecycle, "terminate_agent_op", _fake_terminate)
    result = await lifecycle.lifecycle_op(
        database,
        event_bus,
        "/api/agents/42/terminate",
        {"source": "user"},
        stub_pool,  # type: ignore[arg-type]
    )
    # lifecycle_op now returns the per-action response model (not its dict form).
    assert result.status == "enqueued"
    assert captured["agent_id"] == 42
    assert captured["force"] is False


@pytest.mark.asyncio
async def test_lifecycle_op_unparseable_path_raises(
    stub_pool: object, database: Database, event_bus: EventBus
) -> None:
    with pytest.raises(ValueError, match="lifecycle path not recognized"):
        await lifecycle.lifecycle_op(
            database,
            event_bus,
            "/api/agents/bogus",
            {},
            stub_pool,  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_guarded_resurrect_path_requires_trigger(
    stub_pool: object, database: Database, event_bus: EventBus
) -> None:
    """The new internal path fails closed when its CAS evidence is missing."""
    with pytest.raises(ValueError, match="requires trigger inbound"):
        await lifecycle.lifecycle_op(
            database,
            event_bus,
            "/api/agents/42/resurrect-if-pending-work-v2",
            {"resurrected_by": "system"},
            stub_pool,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    "new_path",
    [
        "/api/agents/42/resurrect-explicit-v2",
        "/api/agents/42/resurrect-if-pending-work-v2",
    ],
)
def test_versioned_resurrect_paths_are_unknown_to_legacy_runner(new_path: str) -> None:
    """Freeze the pre-v2 runner parser: a new gateway's two versioned paths
    cannot match its legacy lifecycle regex, so rollout skew fails closed."""
    legacy_lifecycle_path = re.compile(
        r"^/api/agents/(?P<id>\d+)/(?P<action>terminate|resurrect|restart)$"
    )

    assert legacy_lifecycle_path.fullmatch(new_path) is None
    assert legacy_lifecycle_path.fullmatch("/api/agents/42/resurrect") is not None


@pytest.mark.asyncio
async def test_manual_lifecycle_path_rejects_auto_resurrect_trigger(
    stub_pool: object, database: Database, event_bus: EventBus
) -> None:
    """A mismatched path/guard pair cannot silently fall back to an
    unconditional manual resurrect."""
    with pytest.raises(ValueError, match="only valid for resurrect-if-pending-work-v2"):
        await lifecycle.lifecycle_op(
            database,
            event_bus,
            "/api/agents/42/resurrect",
            {"resurrected_by": "system"},
            stub_pool,  # type: ignore[arg-type]
            trigger_inbound_id=99,
            trigger_inbound_kind=InboundKind.CHAT,
        )


@pytest.mark.asyncio
async def test_legacy_resurrect_path_fails_closed_without_trigger(
    stub_pool: object, database: Database, event_bus: EventBus
) -> None:
    """A new runner rejects an old gateway's ambiguous resurrection even when
    no new trigger field is present; mixed-version rollback cannot revive."""
    with pytest.raises(ValueError, match="legacy /resurrect is refused"):
        await lifecycle.lifecycle_op(
            database,
            event_bus,
            "/api/agents/42/resurrect",
            {"resurrected_by": "user"},
            stub_pool,  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_explicit_v2_resurrect_dispatches_manual_op(
    monkeypatch: pytest.MonkeyPatch, stub_pool: object, database: Database, event_bus: EventBus
) -> None:
    """The versioned unguarded path is the only runner path used by a new
    gateway for a deliberate manual or system lifecycle resurrection."""

    captured: dict[str, object] = {}

    async def _fake_resurrect(
        _db: object,
        _bus: object,
        agent_id: int,
        body: ResurrectAgentRequest,
        *,
        trigger_inbound_id: int | None = None,
        trigger_inbound_kind: str | None = None,
    ) -> ResurrectAgentResponse:
        captured.update(
            agent_id=agent_id,
            resurrected_by=body.resurrected_by,
            trigger_inbound_id=trigger_inbound_id,
            trigger_inbound_kind=trigger_inbound_kind,
        )
        return ResurrectAgentResponse(status=ResurrectResult.SPAWNED)

    monkeypatch.setattr(lifecycle, "resurrect_agent_op", _fake_resurrect)

    result = await lifecycle.lifecycle_op(
        database,
        event_bus,
        "/api/agents/42/resurrect-explicit-v2",
        {"resurrected_by": "user"},
        stub_pool,  # type: ignore[arg-type]
    )

    assert result.status == "spawned"
    assert captured == {
        "agent_id": 42,
        "resurrected_by": "user",
        "trigger_inbound_id": None,
        "trigger_inbound_kind": None,
    }
