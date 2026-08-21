"""`gateway/ops_*.py` — ops-server-callable RPC implementations.

Free functions backing both FastAPI handlers in gateway/app.py and the
in-process dispatch in services/agent_ops/daemon.py. These tests pin the
contract independently of either entry point: dispatch routing in the
ops server has its own coverage in tests/services/agent_ops/test_daemon.py,
endpoint smoke tests live in tests/gateway/test_cluster_endpoints.py.
"""

from __future__ import annotations

import re
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from ops import ops_cluster, ops_lifecycle
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
    process. The schema reuses shared.envelope.validate_source so the legal set
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


@pytest.fixture
def stub_pool() -> object:
    """Sentinel pool — every op call below mocks the gateway/agents helpers so
    the pool is never touched, but the signature still requires an object."""
    return object()


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_launch_agent_op_launches_precreated_row(
    monkeypatch: pytest.MonkeyPatch, stub_pool: object
) -> None:
    """launch_agent_op launches the pre-created row (the #1236 runner-side
    half): _launch_agent_process off the event loop with the config +
    birth_config the gateway stamped, then schedule_launch_confirm."""
    launched: dict[str, object] = {}
    confirmed: list[int] = []

    def _fake_launch(
        agent_id: int,  # pyright: ignore[reportUnknownParameterType]
        config: object = None,  # pyright: ignore[reportUnknownParameterType]
        *,
        birth_config: object = None,  # pyright: ignore[reportUnknownParameterType]
        confirm: bool = False,
    ) -> None:
        launched["agent_id"] = agent_id
        launched["config"] = config
        launched["birth_config"] = birth_config
        launched["confirm"] = confirm

    monkeypatch.setattr(ops_lifecycle.agent_launch, "_launch_agent_process", _fake_launch)
    monkeypatch.setattr(  # pyright: ignore[reportUnknownArgumentType]
        ops_lifecycle.agent_launch, "schedule_launch_confirm", confirmed.append
    )
    body = LaunchAgentRequest(
        agent_id=7,
        config={"llm_model": "gpt-5.6-sol"},
        birth_config={"llm_model": "gpt-5.6-sol"},
    )
    result = await ops_lifecycle.launch_agent_op(body, stub_pool)  # type: ignore[arg-type]
    assert result.id == 7
    assert launched["agent_id"] == 7
    assert launched["config"] == {"llm_model": "gpt-5.6-sol"}
    assert launched["birth_config"] == {"llm_model": "gpt-5.6-sol"}
    assert launched["confirm"] is False
    assert confirmed == [7]


@pytest.mark.asyncio
async def test_launch_agent_op_delivers_plain_spawn_prompt(
    monkeypatch: pytest.MonkeyPatch, stub_pool: object
) -> None:
    """A plain spawn's first prompt is inserted + InboundArrived published on
    the runner side after launch (inbound INSERT is within the runner role)."""
    monkeypatch.setattr(ops_lifecycle.agent_launch, "_launch_agent_process", lambda *_a, **_k: None)  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    monkeypatch.setattr(ops_lifecycle.agent_launch, "schedule_launch_confirm", lambda _id: None)  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    seen: dict[str, object] = {}

    def _fake_insert(_pool: object, agent_id: int, prompt: str, source: str) -> int:
        seen["aid"] = agent_id
        seen["prompt"] = prompt
        seen["source"] = source
        return 11

    monkeypatch.setattr(ops_lifecycle, "_insert_prompt_blocking", _fake_insert)
    published: list[object] = []

    async def _fake_publish(aid: int, iid: int, kind: str, source: str, prompt: str) -> None:
        published.append((aid, iid, kind, source, prompt))

    monkeypatch.setattr(ops_lifecycle, "publish_inbound_arrived", _fake_publish)

    body = LaunchAgentRequest(agent_id=9, prompt="go do X", prompt_source="user", label="runner")
    result = await ops_lifecycle.launch_agent_op(body, stub_pool)  # type: ignore[arg-type]
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
    """A fork's prompt was already delivered pre-launch by create_agent_row —
    the launch op must not insert a second inbound."""
    monkeypatch.setattr(ops_lifecycle.agent_launch, "_launch_agent_process", lambda *_a, **_k: None)  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    monkeypatch.setattr(ops_lifecycle.agent_launch, "schedule_launch_confirm", lambda _id: None)  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    inserted: list[int] = []

    def _fake_insert(_pool: object, _agent_id: int, _prompt: str, _source: str) -> int:
        inserted.append(1)
        return 0

    monkeypatch.setattr(ops_lifecycle, "_insert_prompt_blocking", _fake_insert)
    monkeypatch.setattr(ops_lifecycle, "publish_inbound_arrived", lambda *_a, **_k: None)  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]

    body = LaunchAgentRequest(agent_id=10)  # no prompt — a fork
    result = await ops_lifecycle.launch_agent_op(body, stub_pool)  # type: ignore[arg-type]
    assert result.id == 10
    assert inserted == []


class TestSpawnPrechecksBlocking:
    """The gateway-side prechecks (fork checkpoint resolution) that used to run
    inside spawn_agent_op — now called by create_and_launch_agent before the row
    INSERT."""

    class _FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, *_a):  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
            return False

    class _FakeConn:
        def cursor(self):
            return TestSpawnPrechecksBlocking._FakeCursor()

        def __enter__(self):
            return self

        def __exit__(self, *_a):  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
            return False

    class _FakePool:
        def connection(self):
            return TestSpawnPrechecksBlocking._FakeConn()

    def test_fork_resolves_checkpoint(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """fork_from -> latest_checkpoint_id resolves to an explicit id, not 'latest'."""
        monkeypatch.setattr(ops_lifecycle, "latest_checkpoint_id", lambda _cur, _aid: "ckpt:v1")  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
        checkpoint = ops_lifecycle._spawn_prechecks_blocking(
            SpawnAgentRequest(spawner="user", fork_from=3),
            self._FakePool(),  # type: ignore[arg-type]
        )
        assert checkpoint == "ckpt:v1"

    def test_fork_empty_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """fork_from with no checkpoint raises ForkSourceEmpty (wire-mapped to 409)."""
        from shared.agents import ForkSourceEmpty

        monkeypatch.setattr(ops_lifecycle, "latest_checkpoint_id", lambda _cur, _aid: None)  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
        with pytest.raises(ForkSourceEmpty):
            ops_lifecycle._spawn_prechecks_blocking(
                SpawnAgentRequest(spawner="user", fork_from=3),
                self._FakePool(),  # type: ignore[arg-type]
            )

    def test_plain_spawn_no_checkpoint_lookup(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No fork_from -> no checkpoint lookup, returns None."""
        looked_up: list[object] = []

        def _fake_lookup(_cur: object, _aid: object) -> str:
            looked_up.append(1)
            return "never"

        monkeypatch.setattr(ops_lifecycle, "latest_checkpoint_id", _fake_lookup)
        checkpoint = ops_lifecycle._spawn_prechecks_blocking(
            SpawnAgentRequest(spawner="user"),
            self._FakePool(),  # type: ignore[arg-type]
        )
        assert checkpoint is None
        assert looked_up == []


async def test_restart_agent_op_terminated_short_circuits(
    monkeypatch: pytest.MonkeyPatch, stub_pool: object
) -> None:
    from shared.agents import AgentStatus

    monkeypatch.setattr(ops_lifecycle, "get_agent_status", lambda _aid: AgentStatus.TERMINATED)  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    resp = await ops_lifecycle.restart_agent_op(
        9,
        RestartAgentRequest(source="user"),
        stub_pool,  # type: ignore[arg-type]
    )
    assert resp.status == "already_terminated"


@pytest.mark.asyncio
async def test_resurrect_agent_op_alive_returns_already_alive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from shared.agents import AgentStatus

    monkeypatch.setattr(ops_lifecycle, "get_agent_status", lambda _aid: AgentStatus.RUNNING)  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    resp = await ops_lifecycle.resurrect_agent_op(9, ResurrectAgentRequest(prompt="test"))
    assert resp.status == "already_alive"


@pytest.mark.asyncio
async def test_resurrect_agent_op_stale_trigger_returns_idempotent_noop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The internal guarded path treats a stale chat as an expected no-launch
    race, while leaving the still-terminated status for its caller to return."""
    from ops.agent_wake import ResurrectTriggerStaleError
    from shared.agents import AgentStatus

    def _terminated(_agent_id: int) -> AgentStatus:
        return AgentStatus.TERMINATED

    monkeypatch.setattr(ops_lifecycle, "get_agent_status", _terminated)

    def _stale(*_args: object, **_kwargs: object) -> None:
        raise ResurrectTriggerStaleError("trigger chat no longer qualifies")

    monkeypatch.setattr(ops_lifecycle, "resurrect_agent", _stale)
    resp = await ops_lifecycle.resurrect_agent_op(
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
    from shared.agents import AgentStatus

    monkeypatch.setattr(ops_lifecycle, "get_agent_status", lambda _aid: AgentStatus.TERMINATED)  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    resp = await ops_lifecycle.terminate_agent_op(
        9,
        TerminateAgentRequest(),
        stub_pool,  # type: ignore[arg-type]
    )
    assert resp.status == "already_terminated"


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

    monkeypatch.setattr(ops_lifecycle, "terminate_agent_op", _fake_terminate)  # pyright: ignore[reportUnknownArgumentType]
    result = await ops_lifecycle.lifecycle_op(
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
        await ops_lifecycle.lifecycle_op(
            "/api/agents/bogus",
            {},
            stub_pool,  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_guarded_resurrect_path_requires_trigger(stub_pool: object) -> None:
    """The new internal path fails closed when its CAS evidence is missing."""
    with pytest.raises(ValueError, match="requires trigger inbound"):
        await ops_lifecycle.lifecycle_op(
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
        await ops_lifecycle.lifecycle_op(
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
        await ops_lifecycle.lifecycle_op(
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

    monkeypatch.setattr(ops_lifecycle, "resurrect_agent_op", _fake_resurrect)

    result = await ops_lifecycle.lifecycle_op(
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


def test_cluster_stop_op_invokes_pause(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[bool] = []
    monkeypatch.setattr(ops_cluster, "pause_local_cluster", lambda: called.append(True))
    result = ops_cluster.cluster_stop_op()
    assert result == {}
    assert called == [True]


def test_cluster_resume_op_invokes_unpause(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[bool] = []
    monkeypatch.setattr(ops_cluster, "unpause_local_cluster", lambda: called.append(True))
    result = ops_cluster.cluster_resume_op()
    assert result == {}
    assert called == [True]


def test_cluster_update_op_returns_session_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    expected = {"session": "ava-updater", "log": "/var/log/updater-123.log"}
    monkeypatch.setattr(ops_cluster, "spawn_update", lambda **_kw: expected)  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    assert ops_cluster.cluster_update_op() == expected


def test_cluster_update_op_forwards_restart_only(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}
    monkeypatch.setattr(
        ops_cluster,
        "spawn_update",
        lambda *, restart_only=False, target_sha=None, **_kw: (  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
            seen.update(ro=restart_only, sha=target_sha) or {}  # pyright: ignore[reportUnknownArgumentType]
        ),
    )
    ops_cluster.cluster_update_op(restart_only=True)
    assert seen["ro"] is True


def test_cluster_update_op_forwards_target_sha(monkeypatch: pytest.MonkeyPatch) -> None:
    """cluster_update_op threads the pinned target_sha through to spawn_update."""
    seen: dict[str, object] = {}
    monkeypatch.setattr(
        ops_cluster,
        "spawn_update",
        lambda *, restart_only=False, target_sha=None, **_kw: (  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
            seen.update(ro=restart_only, sha=target_sha) or {}  # pyright: ignore[reportUnknownArgumentType]
        ),
    )
    ops_cluster.cluster_update_op(target_sha="PINNEDSHA")
    assert seen["sha"] == "PINNEDSHA"


def test_cluster_update_op_unpauses_on_pre_spawn_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """spawn_update raising before it manages to spawn ava-updater (e.g. a
    MigrationLayoutError from the validate-before-kill vet) must not leave this
    host paused forever — cluster_update_op self-heals by unpausing locally
    instead of waiting on the gateway's compensating resume or the 10-minute
    stranded-pause watchdog. The original exception still propagates."""

    def _boom(**_kw: object) -> dict[str, str]:
        raise ValueError("migrations layout broken")

    monkeypatch.setattr(ops_cluster, "spawn_update", _boom)
    called: list[bool] = []
    monkeypatch.setattr(ops_cluster, "unpause_local_cluster", lambda: called.append(True))

    with pytest.raises(ValueError, match="migrations layout broken"):
        ops_cluster.cluster_update_op()
    assert called == [True]


def test_cluster_update_op_in_progress_does_not_unpause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ClusterUpdateInProgress means a real update/rollout/restart already owns
    this host's pause — cluster_update_op must not touch it."""
    from ops.cluster import ClusterUpdateInProgress

    def _boom(**_kw: object) -> dict[str, str]:
        raise ClusterUpdateInProgress("ava-updater already running")

    monkeypatch.setattr(ops_cluster, "spawn_update", _boom)
    called: list[bool] = []
    monkeypatch.setattr(ops_cluster, "unpause_local_cluster", lambda: called.append(True))

    with pytest.raises(ClusterUpdateInProgress):
        ops_cluster.cluster_update_op()
    assert called == []


def test_cluster_update_op_success_does_not_unpause(monkeypatch: pytest.MonkeyPatch) -> None:
    """The happy path never touches unpause_local_cluster — that stays the job of
    the spawned ava-updater session's own `ava restart`/`ava start` tail."""
    expected = {"session": "ava-updater", "log": "/var/log/updater-123.log"}
    monkeypatch.setattr(ops_cluster, "spawn_update", lambda **_kw: expected)  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    called: list[bool] = []
    monkeypatch.setattr(ops_cluster, "unpause_local_cluster", lambda: called.append(True))

    assert ops_cluster.cluster_update_op() == expected
    assert called == []


def test_cluster_rollout_op_returns_session_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    expected = {"session": "ava-rollout", "log": "/var/log/rollout-123.log"}
    monkeypatch.setattr(ops_cluster, "spawn_rollout", lambda _origin, **_kw: expected)  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    assert ops_cluster.cluster_rollout_op("test-origin") == expected


def test_cluster_restart_op_returns_session_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    expected = {"session": "ava-cluster-restart", "log": "/var/log/cluster-restart-123.log"}
    monkeypatch.setattr(ops_cluster, "spawn_restart", lambda _origin, **_kw: expected)  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    assert ops_cluster.cluster_restart_op("test-origin") == expected


def test_cluster_update_check_op_returns_check(monkeypatch: pytest.MonkeyPatch) -> None:
    from ops.cluster import UpdateCheck

    chk = UpdateCheck(behind=2, frontend_changed=True, backend_changed=False)
    monkeypatch.setattr(ops_cluster, "update_check", lambda: chk)
    assert ops_cluster.cluster_update_check_op() is chk


def test_cluster_status_op_returns_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    from ops.cluster import ClusterStatus

    snap = ClusterStatus(
        machine_name="wsl", serve_gateway=False, serve_agent_runner=True, paused=False
    )
    monkeypatch.setattr(ops_cluster, "status_snapshot", lambda: snap)
    assert ops_cluster.cluster_status_op() is snap


@pytest.mark.asyncio
async def test_mark_agent_exited_op_rowcount_gt_one_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """rowcount > 1 means the agents_meta PK (one row per id) is violated —
    fail-loud RuntimeError. The PK makes this impossible in practice, so a
    faked cursor.rowcount=2 is the only way to exercise the guard. (The
    status-respecting WHERE-IN guard itself is covered against a real DB in
    tests/gateway/test_agents_internals.py:TestExitedEndpoint.)"""
    monkeypatch.setattr(ops_lifecycle, "list_open_page_names", lambda _conn, _aid: [])  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    fake_cursor = MagicMock()
    fake_cursor.rowcount = 2
    fake_conn = MagicMock()
    fake_conn.cursor.return_value.__enter__.return_value = fake_cursor
    fake_pool = MagicMock()
    fake_pool.connection.return_value.__enter__.return_value = fake_conn
    with pytest.raises(RuntimeError, match="PK invariant violated"):
        await ops_lifecycle.mark_agent_exited_op(42, fake_pool)


class TestResurrectIfTerminatedPlacement:
    """`resurrect_if_terminated` must run the resurrect on the agent's home
    machine (`agents_meta.machine`): local in-process, remote via a 'lifecycle'
    op to that host's ops server. Launching locally for a remote-homed agent
    trips the boot placement gate and crash-loops (the agent-1513 incident);
    an unreachable home machine skips the resurrect — the inbound is already
    queued, so the next delivery or a manual resurrect picks it up."""

    @pytest.mark.asyncio
    async def test_local_home_resurrects_in_process(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Local-homed resurrect dispatches to ops server first; falls back
        to in-process when the ops server is unreachable."""
        from shared.agents import AgentStatus

        statuses = iter([AgentStatus.TERMINATED, AgentStatus.IDLING])
        monkeypatch.setattr(ops_lifecycle, "get_agent_status", lambda _aid: next(statuses))  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
        monkeypatch.setattr(ops_lifecycle, "get_agent_machine", lambda _aid: "home-a")  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
        monkeypatch.setattr(ops_lifecycle, "machine_name", lambda: "home-a")
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

        monkeypatch.setattr(ops_lifecycle, "resurrect_agent_op", _fake_resurrect_op)

        async def _fake_dispatch(*args: object, **kwargs: object) -> dict:  # pyright: ignore[reportMissingTypeArgument, reportUnknownParameterType]
            dispatch_called.append(kwargs)
            raise ops_lifecycle._cluster_rpc.ClusterOpUnreachable("ops server not reachable")

        monkeypatch.setattr(ops_lifecycle._cluster_rpc, "dispatch_to_machine", _fake_dispatch)  # pyright: ignore[reportUnknownArgumentType]

        status = await ops_lifecycle.resurrect_if_terminated(
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

    @pytest.mark.asyncio
    async def test_remote_home_forwards_lifecycle_op(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from shared.agents import AgentStatus

        statuses = iter([AgentStatus.TERMINATED, AgentStatus.IDLING])
        monkeypatch.setattr(ops_lifecycle, "get_agent_status", lambda _aid: next(statuses))  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
        monkeypatch.setattr(ops_lifecycle, "get_agent_machine", lambda _aid: "wsl")  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
        monkeypatch.setattr(ops_lifecycle, "machine_name", lambda: "gateway-host")

        async def _no_local(*_a: object, **_kw: object) -> ResurrectAgentResponse:
            raise AssertionError("remote-homed resurrect must not launch locally")

        monkeypatch.setattr(ops_lifecycle, "resurrect_agent_op", _no_local)
        captured: dict[str, object] = {}

        async def _fake_dispatch(  # pyright: ignore[reportUnknownParameterType]
            target_machine: str,
            kind: str,
            payload: dict,  # pyright: ignore[reportMissingTypeArgument, reportUnknownParameterType]
            **_kw: object,  # pyright: ignore[reportMissingTypeArgument, reportUnknownParameterType]
        ) -> dict:  # pyright: ignore[reportMissingTypeArgument]
            captured.update(target=target_machine, kind=kind, payload=payload)  # pyright: ignore[reportUnknownArgumentType]
            return {"status": "spawned"}  # pyright: ignore[reportUnknownVariableType]

        monkeypatch.setattr(ops_lifecycle._cluster_rpc, "dispatch_to_machine", _fake_dispatch)  # pyright: ignore[reportUnknownArgumentType]

        status = await ops_lifecycle.resurrect_if_terminated(
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
        from ops.cluster_rpc import ClusterOpUnreachable
        from shared.agents import AgentStatus

        monkeypatch.setattr(ops_lifecycle, "get_agent_status", lambda _aid: AgentStatus.TERMINATED)  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
        monkeypatch.setattr(ops_lifecycle, "get_agent_machine", lambda _aid: "wsl")  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
        monkeypatch.setattr(ops_lifecycle, "machine_name", lambda: "gateway-host")

        async def _unreachable(*_a: object, **_kw: object) -> dict:  # pyright: ignore[reportMissingTypeArgument, reportUnknownParameterType]
            raise ClusterOpUnreachable("ops server for machine='wsl' unreachable")

        monkeypatch.setattr(ops_lifecycle._cluster_rpc, "dispatch_to_machine", _unreachable)  # pyright: ignore[reportUnknownArgumentType]

        with caplog.at_level("WARNING"):
            status = await ops_lifecycle.resurrect_if_terminated(
                7, trigger_inbound_id=99, trigger_inbound_kind="chat"
            )
        assert status is AgentStatus.TERMINATED
        assert "home machine unreachable" in caplog.text

    @pytest.mark.asyncio
    async def test_remote_op_failure_swallowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from ops.cluster_rpc import ClusterOpFailed
        from shared.agents import AgentStatus

        monkeypatch.setattr(ops_lifecycle, "get_agent_status", lambda _aid: AgentStatus.TERMINATED)  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
        monkeypatch.setattr(ops_lifecycle, "get_agent_machine", lambda _aid: "wsl")  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
        monkeypatch.setattr(ops_lifecycle, "machine_name", lambda: "gateway-host")

        async def _failed(*_a: object, **_kw: object) -> dict:  # pyright: ignore[reportMissingTypeArgument, reportUnknownParameterType]
            raise ClusterOpFailed({"error": "launch failed on the home machine"})

        monkeypatch.setattr(ops_lifecycle._cluster_rpc, "dispatch_to_machine", _failed)  # pyright: ignore[reportUnknownArgumentType]

        status = await ops_lifecycle.resurrect_if_terminated(
            7, trigger_inbound_id=99, trigger_inbound_kind="chat"
        )
        assert status is AgentStatus.TERMINATED

    @pytest.mark.asyncio
    async def test_not_terminated_short_circuits(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from shared.agents import AgentStatus

        monkeypatch.setattr(ops_lifecycle, "get_agent_status", lambda _aid: AgentStatus.RUNNING)  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]

        def _no_machine_read(_aid: int) -> str:
            raise AssertionError("a live agent must not trigger a machine lookup")

        monkeypatch.setattr(ops_lifecycle, "get_agent_machine", _no_machine_read)

        status = await ops_lifecycle.resurrect_if_terminated(
            5, trigger_inbound_id=99, trigger_inbound_kind="chat"
        )
        assert status is AgentStatus.RUNNING
