# pyright: reportUnknownArgumentType=warning, reportUnknownLambdaType=warning
"""The `ops` op clusters — ops-server-callable RPC implementations.

Free functions backing both the gateway FastAPI handlers and the in-process dispatch in
services/agent_ops/daemon.py. These tests pin the contract independently of either entry point:
dispatch routing in the ops server has its own coverage in services/agent_ops/tests/test_daemon.py,
endpoint smoke tests live in tests/gateway/test_cluster_endpoints.py.
"""

from __future__ import annotations

import re
from unittest.mock import AsyncMock

import psycopg
import pytest
from pydantic import ValidationError

from base.deploy.maintenance.tests.test_admission import isolate as isolate
from ops import cluster, cluster_rpc, lifecycle
from ops.lifecycle import launch
from ops.rpc_schemas import (
    LaunchAgentRequest,
    RestartAgentRequest,
    ResurrectAgentRequest,
    ResurrectAgentResponse,
    SpawnAgentRequest,
    TerminateAgentRequest,
)


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


@pytest.mark.asyncio
async def test_legacy_launch_agent_op_delivers_plain_spawn_prompt(
    monkeypatch: pytest.MonkeyPatch, stub_pool: object
) -> None:
    """An old gateway can still send its plain prompt during a rolling update."""
    seen: dict[str, object] = {}

    def _fake_insert(_pool: object, agent_id: int, prompt: str, source: str) -> int:
        seen["aid"] = agent_id
        seen["prompt"] = prompt
        seen["source"] = source
        return 11

    monkeypatch.setattr(launch, "_insert_prompt_blocking", _fake_insert)
    published: list[object] = []

    async def _fake_publish(aid: int, iid: int, kind: str, source: str, prompt: str) -> None:
        published.append((aid, iid, kind, source, prompt))

    monkeypatch.setattr(lifecycle, "publish_inbound_arrived", _fake_publish)

    body = LaunchAgentRequest(agent_id=9, prompt="go do X", prompt_source="user", label="runner")
    result = await lifecycle.launch_agent_op(body, stub_pool)  # type: ignore[arg-type]
    assert result.id == 9
    assert seen["aid"] == 9
    assert seen["source"] == "user"
    prompt = str(seen["prompt"])
    assert "go do X" in prompt
    assert "runner" in prompt  # the label rides the first prompt
    assert published == [(9, 11, "chat", "user", prompt)]


@pytest.mark.asyncio
async def test_launch_agent_op_skips_prompt_for_fork(
    monkeypatch: pytest.MonkeyPatch, stub_pool: object
) -> None:
    """An old gateway's fork prompt was already delivered before launch."""
    inserted: list[int] = []

    def _fake_insert(_pool: object, _agent_id: int, _prompt: str, _source: str) -> int:
        inserted.append(1)
        return 0

    monkeypatch.setattr(launch, "_insert_prompt_blocking", _fake_insert)
    monkeypatch.setattr(lifecycle, "publish_inbound_arrived", lambda *_a, **_k: None)

    body = LaunchAgentRequest(agent_id=10)  # no prompt — a fork
    result = await lifecycle.launch_agent_op(body, stub_pool)  # type: ignore[arg-type]
    assert result.id == 10
    assert inserted == []


class TestSpawnPrechecksBlocking:
    """The gateway-side prechecks (fork checkpoint resolution) that used to run
    inside spawn_agent_op — now called by create_and_launch_agent before the row
    INSERT."""

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
    monkeypatch: pytest.MonkeyPatch, db_conn: psycopg.Connection
) -> None:
    from tests.fixtures.units import spawn_agent
    from tests.gateway.test_agents_internals import _test_pool

    agent_id = spawn_agent()
    db_conn.execute("UPDATE agents_meta SET status='terminated' WHERE id=%s", (agent_id,))
    db_conn.commit()
    wake = AsyncMock()
    monkeypatch.setattr(lifecycle, "publish_inbound_arrived", wake)
    with _test_pool() as pool:
        resp = await lifecycle.restart_agent_op(agent_id, RestartAgentRequest(source="user"), pool)
    assert resp.status == "already_terminated"
    wake.assert_not_awaited()
    assert db_conn.execute(
        "SELECT count(*) FROM inbound_messages WHERE agent_id=%s AND kind='restart'", (agent_id,)
    ).fetchone() == (0,)


@pytest.mark.asyncio
async def test_restart_lifecycle_op_validates_overlay_on_the_runner(
    stub_pool: object,
) -> None:
    """The runner reparses forwarded restart bodies before any DB write."""
    with pytest.raises(ValidationError):
        await lifecycle.lifecycle_op(
            "/api/agents/9/restart",
            {"config_overlay": {"definitely_not_a_config_field": "x"}},
            stub_pool,  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_resurrect_agent_op_alive_returns_already_alive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from base.agents import AgentStatus

    monkeypatch.setattr(lifecycle, "get_agent_status", lambda _aid: AgentStatus.RUNNING)
    resp = await lifecycle.resurrect_agent_op(9, ResurrectAgentRequest(prompt="test"))
    assert resp.status == "already_alive"


@pytest.mark.asyncio
async def test_resurrect_agent_op_stale_trigger_returns_idempotent_noop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The internal guarded path treats a stale chat as an expected no-launch
    race, while leaving the still-terminated status for its caller to return."""
    from base.agents import AgentStatus
    from ops.agents.wake import ResurrectTriggerStaleError

    def _terminated(_agent_id: int) -> AgentStatus:
        return AgentStatus.TERMINATED

    monkeypatch.setattr(lifecycle, "get_agent_status", _terminated)

    def _stale(*_args: object, **_kwargs: object) -> None:
        raise ResurrectTriggerStaleError("trigger chat no longer qualifies")

    monkeypatch.setattr(lifecycle, "resurrect_agent", _stale)
    resp = await lifecycle.resurrect_agent_op(
        9,
        ResurrectAgentRequest(resurrected_by="system"),
        trigger_inbound_id=123,
        trigger_inbound_kind="chat",
    )
    assert resp.status == "already_alive"


@pytest.mark.asyncio
async def test_terminate_agent_op_terminated_short_circuits(
    monkeypatch: pytest.MonkeyPatch, stub_pool: object
) -> None:
    from base.agents import AgentStatus

    monkeypatch.setattr(lifecycle, "get_agent_status", lambda _aid: AgentStatus.TERMINATED)

    def _no_kill(_aid: int) -> list[int]:
        raise AssertionError("a terminate without the option never kills shell sessions")

    monkeypatch.setattr(lifecycle, "kill_agent_shells", _no_kill)
    resp = await lifecycle.terminate_agent_op(
        9,
        TerminateAgentRequest(),
        stub_pool,  # type: ignore[arg-type]
    )
    assert resp.status == "already_terminated"
    assert resp.shell_sessions is None


@pytest.mark.asyncio
async def test_lifecycle_op_parses_path_to_terminate(
    monkeypatch: pytest.MonkeyPatch, stub_pool: object
) -> None:

    captured: dict[str, object] = {}

    async def _fake_terminate(agent_id, body, pool):  # type: ignore[no-untyped-def]
        captured["agent_id"] = agent_id
        captured["force"] = body.force  # pyright: ignore[reportUnknownMemberType]
        from ops.rpc_schemas import TerminateAgentResponse

        return TerminateAgentResponse(status="enqueued")

    monkeypatch.setattr(lifecycle, "terminate_agent_op", _fake_terminate)
    result = await lifecycle.lifecycle_op(
        "/api/agents/42/terminate",
        {"source": "user"},
        stub_pool,  # type: ignore[arg-type]
    )
    # lifecycle_op now returns the per-action response model (not its dict form).
    assert result.status == "enqueued"
    assert captured["agent_id"] == 42
    assert captured["force"] is False


@pytest.mark.asyncio
async def test_lifecycle_op_unparseable_path_raises(stub_pool: object) -> None:
    with pytest.raises(ValueError, match="lifecycle path not recognized"):
        await lifecycle.lifecycle_op(
            "/api/agents/bogus",
            {},
            stub_pool,  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_guarded_resurrect_path_requires_trigger(stub_pool: object) -> None:
    """The new internal path fails closed when its CAS evidence is missing."""
    with pytest.raises(ValueError, match="requires trigger inbound"):
        await lifecycle.lifecycle_op(
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
async def test_manual_lifecycle_path_rejects_auto_resurrect_trigger(stub_pool: object) -> None:
    """A mismatched path/guard pair cannot silently fall back to an
    unconditional manual resurrect."""
    with pytest.raises(ValueError, match="only valid for resurrect-if-pending-work-v2"):
        await lifecycle.lifecycle_op(
            "/api/agents/42/resurrect",
            {"resurrected_by": "system"},
            stub_pool,  # type: ignore[arg-type]
            trigger_inbound_id=99,
            trigger_inbound_kind="chat",
        )


@pytest.mark.asyncio
async def test_legacy_resurrect_path_fails_closed_without_trigger(stub_pool: object) -> None:
    """A new runner rejects an old gateway's ambiguous resurrection even when
    no new trigger field is present; mixed-version rollback cannot revive."""
    with pytest.raises(ValueError, match="legacy /resurrect is refused"):
        await lifecycle.lifecycle_op(
            "/api/agents/42/resurrect",
            {"resurrected_by": "user"},
            stub_pool,  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_explicit_v2_resurrect_dispatches_manual_op(
    monkeypatch: pytest.MonkeyPatch, stub_pool: object
) -> None:
    """The versioned unguarded path is the only runner path used by a new
    gateway for a deliberate manual or system lifecycle resurrection."""

    captured: dict[str, object] = {}

    async def _fake_resurrect(
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
        return ResurrectAgentResponse(status="spawned")

    monkeypatch.setattr(lifecycle, "resurrect_agent_op", _fake_resurrect)

    result = await lifecycle.lifecycle_op(
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


def test_cluster_status_op_returns_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    from ops.cluster_status import ClusterStatus

    snap = ClusterStatus(
        machine_name="wsl", serve_gateway=False, serve_agent_runner=True, paused=False
    )
    expected_pool = object()
    seen: list[object] = []

    def _snapshot(pool: object | None = None) -> ClusterStatus:
        assert pool is expected_pool
        seen.append(pool)
        return snap

    monkeypatch.setattr(cluster, "status_snapshot", _snapshot)

    assert cluster.cluster_status_op(expected_pool) is snap
    assert seen == [expected_pool]


class TestResurrectIfTerminatedPlacement:
    """`resurrect_if_terminated` must run the resurrect on the agent's home
    machine (`agents_meta.machine`): local in-process, remote via a 'lifecycle'
    op to that host's ops server. Launching locally for a remote-homed agent
    trips the boot placement gate and crash-loops (the agent-1513 incident);
    an unreachable home machine skips the resurrect — the inbound is already
    queued, so the next delivery or a manual resurrect picks it up."""

    @pytest.fixture(autouse=True)
    def _default_unsuppressed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(lifecycle, "_wake_suppression_active", lambda _aid: False)
        monkeypatch.setattr(lifecycle, "_recovery_halted", lambda _aid: False)
        monkeypatch.setattr(lifecycle, "_clear_wake_suppression", lambda _aid: None)
        # The notice guard reads the trigger row from the DB; these tests pin
        # dispatch placement, not the guard — default it to "not a notice".
        monkeypatch.setattr(lifecycle, "_system_notice_source_of_trigger", lambda _aid, _iid: None)

    @pytest.mark.asyncio
    async def test_active_suppression_skips_forward_and_launch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from base.agents import AgentStatus

        monkeypatch.setattr(lifecycle, "get_agent_status", lambda _aid: AgentStatus.TERMINATED)
        monkeypatch.setattr(lifecycle, "_wake_suppression_active", lambda _aid: True)

        def _no_machine_read(_aid: int) -> str:
            raise AssertionError("suppressed auto-resurrect must not read or contact the home")

        monkeypatch.setattr(lifecycle, "get_agent_machine", _no_machine_read)
        status = await lifecycle.resurrect_if_terminated(
            5, trigger_inbound_id=88, trigger_inbound_kind="chat"
        )
        assert status is AgentStatus.TERMINATED

    @pytest.mark.asyncio
    async def test_tripped_recovery_breaker_skips_forward_and_launch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A tripped recovery breaker (consecutive permanent provider
        rejections) refuses the automatic resurrect before any home contact,
        exactly like an active wake suppression (task #3617)."""
        from base.agents import AgentStatus

        monkeypatch.setattr(lifecycle, "get_agent_status", lambda _aid: AgentStatus.TERMINATED)
        monkeypatch.setattr(lifecycle, "_recovery_halted", lambda _aid: True)

        def _no_machine_read(_aid: int) -> str:
            raise AssertionError("halted auto-resurrect must not read or contact the home")

        monkeypatch.setattr(lifecycle, "get_agent_machine", _no_machine_read)
        status = await lifecycle.resurrect_if_terminated(
            5, trigger_inbound_id=88, trigger_inbound_kind="chat"
        )
        assert status is AgentStatus.TERMINATED

    @pytest.mark.asyncio
    async def test_local_home_resurrects_in_process(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Local-homed resurrect dispatches to ops server first; falls back
        to in-process when the ops server is unreachable."""
        from base.agents import AgentStatus

        statuses = iter([AgentStatus.TERMINATED, AgentStatus.IDLING])
        monkeypatch.setattr(lifecycle, "get_agent_status", lambda _aid: next(statuses))
        monkeypatch.setattr(lifecycle, "get_agent_machine", lambda _aid: "home-a")
        monkeypatch.setattr(lifecycle, "machine_name", lambda: "home-a")
        called: dict[str, object] = {}
        dispatch_called: list[dict[str, object]] = []

        async def _fake_resurrect_op(
            agent_id: int,
            body: ResurrectAgentRequest,
            *,
            trigger_inbound_id: int | None = None,
            trigger_inbound_kind: str | None = None,
        ) -> ResurrectAgentResponse:
            called["agent_id"] = agent_id
            called["resurrected_by"] = body.resurrected_by
            called["trigger_inbound_id"] = trigger_inbound_id
            called["trigger_inbound_kind"] = trigger_inbound_kind
            return ResurrectAgentResponse(status="spawned")

        monkeypatch.setattr(lifecycle, "resurrect_agent_op", _fake_resurrect_op)
        cleared: list[int] = []

        def _record_clear(agent_id: int) -> None:
            cleared.append(agent_id)

        monkeypatch.setattr(
            lifecycle,
            "_clear_wake_suppression",
            _record_clear,
        )

        async def _fake_dispatch(*args: object, **kwargs: object) -> dict:
            dispatch_called.append(kwargs)
            raise lifecycle._cluster_rpc.ClusterOpUnreachable("ops server not reachable")

        monkeypatch.setattr(cluster_rpc, "dispatch_to_machine", _fake_dispatch)

        status = await lifecycle.resurrect_if_terminated(
            5,
            trigger_inbound_id=88,
            trigger_inbound_kind="chat",
        )
        assert status is AgentStatus.IDLING
        # Dispatch was attempted (HTTP-uniform path)
        assert len(dispatch_called) == 1
        assert dispatch_called[0]["target_machine"] == "home-a"
        assert dispatch_called[0]["payload"] == {
            "path": "/api/agents/5/resurrect-if-pending-work-v2",
            "body": {"resurrected_by": "system", "prompt": None},
            "trigger_inbound_id": 88,
            "trigger_inbound_kind": "chat",
        }
        # Fallback: in-process resurrect happened
        assert called == {
            "agent_id": 5,
            "resurrected_by": "system",
            "trigger_inbound_id": 88,
            "trigger_inbound_kind": "chat",
        }
        assert cleared == [5]

    @pytest.mark.asyncio
    async def test_remote_home_forwards_lifecycle_op(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, object] = {}
        from base.agents import AgentStatus

        statuses = iter([AgentStatus.TERMINATED, AgentStatus.IDLING])
        monkeypatch.setattr(lifecycle, "get_agent_status", lambda _aid: next(statuses))
        monkeypatch.setattr(lifecycle, "get_agent_machine", lambda _aid: "wsl")
        monkeypatch.setattr(lifecycle, "machine_name", lambda: "gateway-host")

        async def _no_local(*_a: object, **_kw: object) -> ResurrectAgentResponse:
            raise AssertionError("remote-homed resurrect must not launch locally")

        monkeypatch.setattr(lifecycle, "resurrect_agent_op", _no_local)

        async def _fake_dispatch(
            target_machine: str,
            kind: str,
            payload: dict,
            **_kw: object,
        ) -> dict:
            captured.update(target=target_machine, kind=kind, payload=payload)
            return {"status": "spawned"}

        monkeypatch.setattr(cluster_rpc, "dispatch_to_machine", _fake_dispatch)

        status = await lifecycle.resurrect_if_terminated(
            7,
            trigger_inbound_id=99,
            trigger_inbound_kind="chat",
        )
        assert status is AgentStatus.IDLING
        assert captured["target"] == "wsl"
        assert captured["kind"] == "lifecycle"
        assert captured["payload"] == {
            "path": "/api/agents/7/resurrect-if-pending-work-v2",
            "body": {"resurrected_by": "system", "prompt": None},
            "trigger_inbound_id": 99,
            "trigger_inbound_kind": "chat",
        }

    @pytest.mark.asyncio
    async def test_remote_home_unreachable_skips(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        from base.agents import AgentStatus
        from ops.cluster_rpc import ClusterOpUnreachable

        monkeypatch.setattr(lifecycle, "get_agent_status", lambda _aid: AgentStatus.TERMINATED)
        monkeypatch.setattr(lifecycle, "get_agent_machine", lambda _aid: "wsl")
        monkeypatch.setattr(lifecycle, "machine_name", lambda: "gateway-host")

        async def _unreachable(*_a: object, **_kw: object) -> dict:
            raise ClusterOpUnreachable("ops server for machine='wsl' unreachable")

        monkeypatch.setattr(cluster_rpc, "dispatch_to_machine", _unreachable)

        with caplog.at_level("INFO"):
            status = await lifecycle.resurrect_if_terminated(
                7, trigger_inbound_id=99, trigger_inbound_kind="chat"
            )
        assert status is AgentStatus.TERMINATED
        assert "home machine unreachable" in caplog.text

    @pytest.mark.asyncio
    async def test_remote_op_failure_swallowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from base.agents import AgentStatus
        from ops.cluster_rpc import ClusterOpFailed

        monkeypatch.setattr(lifecycle, "get_agent_status", lambda _aid: AgentStatus.TERMINATED)
        monkeypatch.setattr(lifecycle, "get_agent_machine", lambda _aid: "wsl")
        monkeypatch.setattr(lifecycle, "machine_name", lambda: "gateway-host")

        async def _failed(*_a: object, **_kw: object) -> dict:
            raise ClusterOpFailed({"error": "launch failed on the home machine"})

        monkeypatch.setattr(cluster_rpc, "dispatch_to_machine", _failed)

        status = await lifecycle.resurrect_if_terminated(
            7, trigger_inbound_id=99, trigger_inbound_kind="chat"
        )
        assert status is AgentStatus.TERMINATED

    @pytest.mark.asyncio
    async def test_not_terminated_short_circuits(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from base.agents import AgentStatus

        monkeypatch.setattr(lifecycle, "get_agent_status", lambda _aid: AgentStatus.RUNNING)

        def _no_machine_read(_aid: int) -> str:
            raise AssertionError("a live agent must not trigger a machine lookup")

        monkeypatch.setattr(lifecycle, "get_agent_machine", _no_machine_read)

        status = await lifecycle.resurrect_if_terminated(
            5, trigger_inbound_id=99, trigger_inbound_kind="chat"
        )
        assert status is AgentStatus.RUNNING


class TestResurrectIfTerminatedNotificationGuard:
    """A system-family chat trigger never resurrects its owner (user ruling
    2026-08-27; task #3687): the watcher-reap notice that woke 6260 twice is a
    queued notification, not a wake-up call. The guard reads the trigger row
    itself; a missing row falls through to the normal path (the home runner's
    final CAS still adjudicates stale work), and a DB read failure propagates
    instead of silently becoming a skip."""

    @pytest.fixture(autouse=True)
    def _default_state(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(lifecycle, "_wake_suppression_active", lambda _aid: False)
        monkeypatch.setattr(lifecycle, "_recovery_halted", lambda _aid: False)
        monkeypatch.setattr(lifecycle, "_clear_wake_suppression", lambda _aid: None)

    @pytest.mark.asyncio
    async def test_system_notice_trigger_skips_forward_and_launch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from base.agents import AgentStatus

        monkeypatch.setattr(lifecycle, "get_agent_status", lambda _aid: AgentStatus.TERMINATED)
        monkeypatch.setattr(
            lifecycle, "_system_notice_source_of_trigger", lambda _aid, _iid: "system"
        )

        def _no_machine_read(_aid: int) -> str:
            raise AssertionError("a system notice must not read or contact the home")

        monkeypatch.setattr(lifecycle, "get_agent_machine", _no_machine_read)
        status = await lifecycle.resurrect_if_terminated(
            5, trigger_inbound_id=207124, trigger_inbound_kind="chat"
        )
        assert status is AgentStatus.TERMINATED

    @pytest.mark.asyncio
    async def test_missing_trigger_row_falls_through(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No row -> None -> the normal path runs; stale-work adjudication stays
        with the home runner's final CAS. This drives the real read (the id
        does not exist), not a stubbed one."""
        from base.agents import AgentStatus

        statuses = iter([AgentStatus.TERMINATED, AgentStatus.IDLING])
        monkeypatch.setattr(lifecycle, "get_agent_status", lambda _aid: next(statuses))
        monkeypatch.setattr(lifecycle, "get_agent_machine", lambda _aid: "home-a")
        monkeypatch.setattr(lifecycle, "machine_name", lambda: "home-a")
        calls: list[int] = []

        async def _fake_op(
            agent_id: int,
            body: ResurrectAgentRequest,
            *,
            trigger_inbound_id: int | None = None,
            trigger_inbound_kind: str | None = None,
        ) -> ResurrectAgentResponse:
            calls.append(agent_id)
            return ResurrectAgentResponse(status="spawned")

        monkeypatch.setattr(lifecycle, "resurrect_agent_op", _fake_op)

        async def _unreachable(*_a: object, **_kw: object) -> dict:
            raise lifecycle._cluster_rpc.ClusterOpUnreachable("no ops server")

        monkeypatch.setattr(cluster_rpc, "dispatch_to_machine", _unreachable)

        status = await lifecycle.resurrect_if_terminated(
            5, trigger_inbound_id=10**12, trigger_inbound_kind="chat"
        )
        assert status is AgentStatus.IDLING
        assert calls == [5]

    @pytest.mark.asyncio
    async def test_read_failure_propagates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A failed trigger read must not be swallowed into a skip (review
        note A): the error surfaces to the caller, mirroring how a failed
        suppression / breaker read above fails loudly."""
        from base.agents import AgentStatus

        monkeypatch.setattr(lifecycle, "get_agent_status", lambda _aid: AgentStatus.TERMINATED)

        def _boom(_aid: int, _iid: int) -> str | None:
            raise RuntimeError("trigger read failed")

        monkeypatch.setattr(lifecycle, "_system_notice_source_of_trigger", _boom)
        with pytest.raises(RuntimeError, match="trigger read failed"):
            await lifecycle.resurrect_if_terminated(
                5, trigger_inbound_id=207124, trigger_inbound_kind="chat"
            )

    def test_trigger_guard_reads_row_kind_source_and_payload(
        self, db_conn: psycopg.Connection
    ) -> None:
        """The guard reads the row itself: system-family chats are notices
        (plain and variant), a user chat and a system_note are not, and a
        missing row / foreign agent falls through to None. The payload marker
        is a fail-closed carve-out: only the exact JSON boolean `true` lets a
        system-family chat through — a missing key, null, or any other value
        (even the string "true") stays a notice (task #3687 review, Ava #3242)."""
        from base.db import create_agent, insert_inbound_message

        aid = create_agent(db_conn)
        db_conn.commit()
        sys_iid = insert_inbound_message(db_conn, aid, "notice", source="system")
        var_iid = insert_inbound_message(db_conn, aid, "variant", source="system:notice-reply")
        user_iid = insert_inbound_message(db_conn, aid, "hi", source="user")
        note_iid = insert_inbound_message(db_conn, aid, "note", source="system", kind="system_note")
        # A watcher's wake is source="watcher:<id>" — neither "system" nor a
        # "system:" variant, so it is NOT a notice: a terminated owner with a
        # live watcher is auto-resurrected at its next fire, same as any user
        # chat (decisions/2026-09-27-watchers-are-never-restarted.md).
        watcher_iid = insert_inbound_message(db_conn, aid, "wake", source="watcher:7")

        assert lifecycle._system_notice_source_of_trigger(aid, sys_iid) == "system"
        assert lifecycle._system_notice_source_of_trigger(aid, var_iid) == "system:notice-reply"
        assert lifecycle._system_notice_source_of_trigger(aid, user_iid) is None
        assert lifecycle._system_notice_source_of_trigger(aid, note_iid) is None
        assert lifecycle._system_notice_source_of_trigger(aid, watcher_iid) is None
        assert lifecycle._system_notice_source_of_trigger(aid, 10**12) is None
        assert lifecycle._system_notice_source_of_trigger(aid + 999, sys_iid) is None

        recovery_iid = insert_inbound_message(
            db_conn, aid, "continue", source="system", payload={"hosted_turn_recovery": True}
        )
        assert lifecycle._system_notice_source_of_trigger(aid, recovery_iid) is None
        user_marker_iid = insert_inbound_message(
            db_conn, aid, "hi", source="user", payload={"hosted_turn_recovery": True}
        )
        assert lifecycle._system_notice_source_of_trigger(aid, user_marker_iid) is None

        fail_closed: tuple[tuple[str, dict[str, object] | None], ...] = (
            ("payload-absent", None),
            ("key-absent", {"content_blocks": []}),
            ("json-null", {"hosted_turn_recovery": None}),
            ("boolean-false", {"hosted_turn_recovery": False}),
            ("string-true", {"hosted_turn_recovery": "true"}),
            ("number-1", {"hosted_turn_recovery": 1}),
        )
        for label, payload in fail_closed:
            iid = insert_inbound_message(db_conn, aid, "noticed", source="system", payload=payload)
            assert lifecycle._system_notice_source_of_trigger(aid, iid) == "system", label

    @pytest.mark.asyncio
    async def test_hosted_turn_recovery_marker_reaches_dispatch(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """BLOCK regression (Ava #3242): the watchdog's hosted-turn recovery
        chat is kind='chat', source='system' — the plain notice verdict
        silently matched it and both resurrection channels went dark. This
        drives the REAL guard (no stub) against the REAL marker row and
        asserts the chain reaches the resurrect dispatch, with only the
        below-dispatch machinery stubbed; a guard that wrongly matched would
        reach no dispatch and fail the assert."""
        from base.agents import AgentStatus
        from base.db import create_agent, insert_inbound_message

        aid = create_agent(db_conn)
        db_conn.commit()
        rec_iid = insert_inbound_message(
            db_conn,
            aid,
            "continue from the latest checkpoint",
            source="system",
            payload={"hosted_turn_recovery": True},
        )
        statuses = iter([AgentStatus.TERMINATED, AgentStatus.IDLING])
        monkeypatch.setattr(lifecycle, "get_agent_status", lambda _aid: next(statuses))
        monkeypatch.setattr(lifecycle, "get_agent_machine", lambda _aid: "home-a")
        monkeypatch.setattr(lifecycle, "machine_name", lambda: "home-a")
        calls: list[tuple[int, int | None]] = []

        async def _fake_op(
            agent_id: int,
            body: ResurrectAgentRequest,
            *,
            trigger_inbound_id: int | None = None,
            trigger_inbound_kind: str | None = None,
        ) -> ResurrectAgentResponse:
            calls.append((agent_id, trigger_inbound_id))
            return ResurrectAgentResponse(status="spawned")

        monkeypatch.setattr(lifecycle, "resurrect_agent_op", _fake_op)

        async def _unreachable(*_a: object, **_kw: object) -> dict:
            raise lifecycle._cluster_rpc.ClusterOpUnreachable("no ops server")

        monkeypatch.setattr(cluster_rpc, "dispatch_to_machine", _unreachable)

        status = await lifecycle.resurrect_if_terminated(
            aid, trigger_inbound_id=rec_iid, trigger_inbound_kind="chat"
        )
        assert status is AgentStatus.IDLING
        assert calls == [(aid, rec_iid)]


@pytest.mark.asyncio
async def test_spawned_auto_resurrect_clears_suppression_in_database(
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful spawn is a durable recovery, not only an in-memory result."""
    from base.agents import AgentStatus
    from base.db import create_agent, insert_inbound_message

    agent_id = create_agent(db_conn)
    db_conn.execute(
        "INSERT INTO agents_meta "
        "(id,status,machine,wake_suppressed_until,wake_suppress_reason) "
        "VALUES(%s,'terminated','remote-home',now()-interval '1 second','resurrect_failed')",
        (agent_id,),
    )
    db_conn.commit()
    trigger_id = insert_inbound_message(db_conn, agent_id, "recover", source="user")

    async def _spawn_on_home(*_args: object, **_kwargs: object) -> dict[str, str]:
        db_conn.execute("UPDATE agents_meta SET status='idling' WHERE id=%s", (agent_id,))
        db_conn.commit()
        return {"status": "spawned"}

    monkeypatch.setattr(cluster_rpc, "dispatch_to_machine", _spawn_on_home)

    status = await lifecycle.resurrect_if_terminated(
        agent_id,
        trigger_inbound_id=trigger_id,
        trigger_inbound_kind="chat",
    )

    assert status is AgentStatus.IDLING
    assert db_conn.execute(
        "SELECT wake_suppressed_until,wake_suppress_reason FROM agents_meta WHERE id=%s",
        (agent_id,),
    ).fetchone() == (None, None)


@pytest.mark.asyncio
async def test_launch_agent_op_hosted_skips_process_and_wakes(
    monkeypatch: pytest.MonkeyPatch, stub_pool: object
) -> None:
    """Hosted mode: the row the gateway created IS the agent. No fork, no
    launch-confirm — the prompt INSERT (which publishes its own wake inside
    `insert_inbound_message`) plus one explicit wake is the whole launch."""
    inserted: list[tuple[int, str, str]] = []

    def _fake_insert(_pool: object, agent_id: int, prompt: str, source: str) -> int:
        inserted.append((agent_id, prompt, source))
        return 11

    monkeypatch.setattr(launch, "_insert_prompt_blocking", _fake_insert)

    async def _fake_publish(*_a: object, **_k: object) -> None:
        return None

    monkeypatch.setattr(lifecycle, "publish_inbound_arrived", _fake_publish)
    wakes: list[tuple[int, str]] = []
    monkeypatch.setattr(
        launch, "publish_inbound_wake", lambda aid, payload: wakes.append((aid, payload))
    )

    body = LaunchAgentRequest(agent_id=7, prompt="go do X", prompt_source="user")
    result = await lifecycle.launch_agent_op(body, stub_pool)  # type: ignore[arg-type]
    assert result.id == 7
    assert inserted == [(7, "go do X", "user")]
    assert wakes == [(7, "0")]


@pytest.mark.asyncio
async def test_launch_agent_op_hosted_fork_still_wakes(
    monkeypatch: pytest.MonkeyPatch, stub_pool: object
) -> None:
    """A fork's inbounds were pre-inserted by create_agent_row as raw SQL (no
    wake inside) — the hosted launch must publish the wake explicitly, and must
    not insert a second prompt."""
    inserted: list[int] = []

    def _fake_insert(_pool: object, _agent_id: int, _prompt: str, _source: str) -> int:
        inserted.append(1)
        return 0

    monkeypatch.setattr(launch, "_insert_prompt_blocking", _fake_insert)

    async def _fake_publish(*_a: object, **_k: object) -> None:
        return None

    monkeypatch.setattr(lifecycle, "publish_inbound_arrived", _fake_publish)
    wakes: list[tuple[int, str]] = []
    monkeypatch.setattr(
        launch, "publish_inbound_wake", lambda aid, payload: wakes.append((aid, payload))
    )

    body = LaunchAgentRequest(agent_id=8)
    result = await lifecycle.launch_agent_op(body, stub_pool)  # type: ignore[arg-type]
    assert result.id == 8
    assert inserted == []  # fork prompt is delivered pre-launch, never here
    assert wakes == [(8, "0")]


async def test_force_terminate_hosted_skips_process_kill_and_cancels_turn(
    monkeypatch: pytest.MonkeyPatch, stub_pool: object
) -> None:
    """Hosted force-terminate: no process to SIGKILL — the DB fence runs with
    kill_process=False and the turn-cancel acceleration fires after the
    transaction. The durable terminate inbound inserted by the fence is the
    captured: dict[str, object] = {}
    correctness mechanism; the cancel only accelerates a wedged turn."""
    from base.agents import AgentStatus

    captured: dict[str, object] = {}

    def _fake_force_blocking(
        aid: int, _body: object, _pool: object
    ) -> tuple[AgentStatus, int | None, list[str], int]:
        captured["agent_id"] = aid
        return AgentStatus.RUNNING, None, [], 91

    monkeypatch.setattr(lifecycle, "_terminate_force_blocking", _fake_force_blocking)
    cancelled: list[tuple[int, int]] = []

    async def _fake_cancel(aid: int, command_id: int) -> None:
        cancelled.append((aid, command_id))

    monkeypatch.setattr(lifecycle, "_cancel_hosted_turn_best_effort", _fake_cancel)

    async def _fake_page_closed(*_a: object, **_k: object) -> None:
        return None

    monkeypatch.setattr(lifecycle, "publish_page_closed", _fake_page_closed)

    resp = await lifecycle.terminate_agent_op(
        9,
        TerminateAgentRequest(force=True),
        stub_pool,  # type: ignore[arg-type]
    )
    assert resp.status == "enqueued"
    assert resp.shell_sessions is None
    assert captured == {"agent_id": 9}
    assert cancelled == [(9, 91)]


@pytest.mark.asyncio
async def test_launch_agent_op_hosted_failure_preserves_its_row(
    monkeypatch: pytest.MonkeyPatch, stub_pool: object
) -> None:
    """A failed legacy prompt insert leaves the row for explicit repair."""

    def _boom(_pool: object, _agent_id: int, _prompt: str, _source: str) -> int:
        raise RuntimeError("prompt insert failed")

    monkeypatch.setattr(launch, "_insert_prompt_blocking", _boom)
    reclaimed: list[tuple[int, str]] = []

    def _fake_reclaim(agent_id: int, _pool: object, *, source: str) -> list[str]:
        reclaimed.append((agent_id, source))
        return []

    monkeypatch.setattr(lifecycle, "_force_mark_terminated", _fake_reclaim)

    body = LaunchAgentRequest(agent_id=7, prompt="go", prompt_source="user")
    with pytest.raises(RuntimeError, match="prompt insert failed"):
        await lifecycle.launch_agent_op(body, stub_pool)  # type: ignore[arg-type]
    assert reclaimed == []


@pytest.mark.asyncio
async def test_launch_agent_op_hosted_validation_failure_preserves_its_row(
    monkeypatch: pytest.MonkeyPatch, stub_pool: object
) -> None:
    """A runner rejection is reported by the gateway; it never terminates creation."""

    def _boom_validate(*_a: object, **_k: object) -> None:
        raise RuntimeError("bad model config")

    monkeypatch.setattr("base.lm.factory.validate_model_config", _boom_validate)
    reclaimed: list[tuple[int, str]] = []

    def _fake_reclaim(agent_id: int, _pool: object, *, source: str) -> list[str]:
        reclaimed.append((agent_id, source))
        return []

    monkeypatch.setattr(lifecycle, "_force_mark_terminated", _fake_reclaim)

    body = LaunchAgentRequest(agent_id=7, prompt="go", prompt_source="user")
    with pytest.raises(RuntimeError, match="bad model config"):
        await lifecycle.launch_agent_op(body, stub_pool)  # type: ignore[arg-type]
    assert reclaimed == []
