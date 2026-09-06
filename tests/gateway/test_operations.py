# pyright: reportUnknownArgumentType=warning, reportUnknownLambdaType=warning
"""`gateway/ops_*.py` — ops-server-callable RPC implementations.

Free functions backing both FastAPI handlers in gateway/app.py and the
in-process dispatch in services/agent_ops/daemon.py. These tests pin the
contract independently of either entry point: dispatch routing in the
ops server has its own coverage in tests/services/agent_ops/test_daemon.py,
endpoint smoke tests live in tests/gateway/test_cluster_endpoints.py.
"""

from __future__ import annotations

import re
from unittest.mock import AsyncMock, MagicMock

import psycopg
import pytest
from pydantic import ValidationError

from ops import ops_cluster, ops_exit, ops_launch, ops_lifecycle
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


class TestRestartAgentRequestConfigOverlay:
    """Restart overlays fail at both HTTP and runner schema boundaries."""

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
        agent_id: int,
        config: object = None,
        *,
        birth_config: object = None,
        confirm: bool = False,
    ) -> None:
        launched["agent_id"] = agent_id
        launched["config"] = config
        launched["birth_config"] = birth_config
        launched["confirm"] = confirm

    monkeypatch.setattr(ops_launch.agent_launch, "_launch_agent_process", _fake_launch)
    monkeypatch.setattr(
        ops_launch.agent_launch,
        "schedule_launch_confirm",
        lambda agent_id, _attempt: confirmed.append(agent_id),
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
    monkeypatch.setattr(ops_launch.agent_launch, "_launch_agent_process", lambda *_a, **_k: None)
    monkeypatch.setattr(
        ops_launch.agent_launch, "schedule_launch_confirm", lambda _id, _attempt=None: None
    )
    seen: dict[str, object] = {}

    def _fake_insert(_pool: object, agent_id: int, prompt: str, source: str) -> int:
        seen["aid"] = agent_id
        seen["prompt"] = prompt
        seen["source"] = source
        return 11

    monkeypatch.setattr(ops_launch, "_insert_prompt_blocking", _fake_insert)
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
    monkeypatch.setattr(ops_launch.agent_launch, "_launch_agent_process", lambda *_a, **_k: None)
    monkeypatch.setattr(
        ops_launch.agent_launch, "schedule_launch_confirm", lambda _id, _attempt=None: None
    )
    inserted: list[int] = []

    def _fake_insert(_pool: object, _agent_id: int, _prompt: str, _source: str) -> int:
        inserted.append(1)
        return 0

    monkeypatch.setattr(ops_launch, "_insert_prompt_blocking", _fake_insert)
    monkeypatch.setattr(ops_lifecycle, "publish_inbound_arrived", lambda *_a, **_k: None)

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
        monkeypatch.setattr(ops_launch, "latest_checkpoint_id", lambda _cur, _aid: "ckpt:v1")
        checkpoint = ops_launch._spawn_prechecks_blocking(
            SpawnAgentRequest(spawner="user", fork_from=3),
            self._FakePool(),  # type: ignore[arg-type]
        )
        assert checkpoint == "ckpt:v1"

    def test_fork_empty_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """fork_from with no checkpoint raises ForkSourceEmpty (wire-mapped to 409)."""
        from shared.agents import ForkSourceEmpty

        monkeypatch.setattr(ops_launch, "latest_checkpoint_id", lambda _cur, _aid: None)
        with pytest.raises(ForkSourceEmpty):
            ops_launch._spawn_prechecks_blocking(
                SpawnAgentRequest(spawner="user", fork_from=3),
                self._FakePool(),  # type: ignore[arg-type]
            )

    def test_plain_spawn_no_checkpoint_lookup(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No fork_from -> no checkpoint lookup, returns None."""
        looked_up: list[object] = []

        def _fake_lookup(_cur: object, _aid: object) -> str:
            looked_up.append(1)
            return "never"

        monkeypatch.setattr(ops_launch, "latest_checkpoint_id", _fake_lookup)
        checkpoint = ops_launch._spawn_prechecks_blocking(
            SpawnAgentRequest(spawner="user"),
            self._FakePool(),  # type: ignore[arg-type]
        )
        assert checkpoint is None
        assert looked_up == []


async def test_restart_agent_op_terminated_short_circuits(
    monkeypatch: pytest.MonkeyPatch, db_conn: psycopg.Connection
) -> None:
    from tests.conftest import spawn_agent
    from tests.gateway.test_agents_internals import _test_pool

    agent_id = spawn_agent()
    db_conn.execute("UPDATE agents_meta SET status='terminated' WHERE id=%s", (agent_id,))
    db_conn.commit()
    wake = AsyncMock()
    monkeypatch.setattr(ops_lifecycle, "publish_inbound_arrived", wake)
    with _test_pool() as pool:
        resp = await ops_lifecycle.restart_agent_op(
            agent_id, RestartAgentRequest(source="user"), pool
        )
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
        await ops_lifecycle.lifecycle_op(
            "/api/agents/9/restart",
            {"config_overlay": {"definitely_not_a_config_field": "x"}},
            stub_pool,  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_resurrect_agent_op_alive_returns_already_alive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from shared.agents import AgentStatus

    monkeypatch.setattr(ops_lifecycle, "get_agent_status", lambda _aid: AgentStatus.RUNNING)
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

    monkeypatch.setattr(ops_lifecycle, "get_agent_status", lambda _aid: AgentStatus.TERMINATED)
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

    monkeypatch.setattr(ops_lifecycle, "terminate_agent_op", _fake_terminate)
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
    from datetime import UTC, datetime

    from shared.cluster_lock import DeployLease

    acquired = datetime(2026, 8, 25, tzinfo=UTC)
    monkeypatch.setattr(
        ops_cluster,
        "read_update_lease",
        lambda: DeployLease(
            holder="gateway:pid1",
            held_for_s=1,
            expires_in_s=600,
            note=None,
            kind="rollout",
            acquired_at=acquired,
        ),
    )
    monkeypatch.setattr(ops_cluster.pause_owner, "mark_paused", lambda *_a: None)
    monkeypatch.setattr(ops_cluster, "pause_local_cluster", lambda: called.append(True))
    result = ops_cluster.cluster_stop_op("gateway:pid1", acquired)
    assert result == {}
    assert called == [True]


@pytest.mark.parametrize("kind", [None, "rollout", "restart"])
def test_cluster_stop_accepts_every_executing_lease_including_legacy_and_rollback(
    kind: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import UTC, datetime

    from shared.cluster_lock import DeployLease

    acquired = datetime(2026, 8, 25, tzinfo=UTC)
    paused: list[bool] = []
    monkeypatch.setattr(
        ops_cluster,
        "read_update_lease",
        lambda: DeployLease(
            holder="gateway:pid1",
            held_for_s=1,
            expires_in_s=600,
            note=None,
            kind=kind,  # type: ignore[arg-type]
            acquired_at=acquired,
        ),
    )
    monkeypatch.setattr(ops_cluster.pause_owner, "mark_paused", lambda *_a: None)
    monkeypatch.setattr(ops_cluster, "pause_local_cluster", lambda: paused.append(True))

    assert ops_cluster.cluster_stop_op("gateway:pid1", acquired) == {}
    assert paused == [True]


def test_cluster_stop_refuses_a_settle_hold_without_pausing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import UTC, datetime

    from ops.cluster import ClusterUpdateInProgress
    from shared.cluster_lock import DeployLease

    acquired = datetime(2026, 8, 25, tzinfo=UTC)
    monkeypatch.setattr(
        ops_cluster,
        "read_update_lease",
        lambda: DeployLease(
            holder="gateway:pid1",
            held_for_s=1,
            expires_in_s=600,
            note="settling, waiting for: win",
            kind="rollout",
            acquired_at=acquired,
        ),
    )
    monkeypatch.setattr(
        ops_cluster,
        "pause_local_cluster",
        lambda: pytest.fail("settle hold cannot authorize a new pause"),
    )
    monkeypatch.setattr(
        ops_cluster.pause_owner,
        "mark_paused",
        lambda *_a: pytest.fail("a mismatched first proof cannot journal a pause owner"),
    )

    with pytest.raises(ClusterUpdateInProgress, match="exact executing deploy lease"):
        ops_cluster.cluster_stop_op("gateway:pid1", acquired)


def test_cluster_stop_clears_its_journal_if_lease_changes_before_pause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import UTC, datetime

    from ops.cluster import ClusterUpdateInProgress
    from shared.cluster_lock import DeployLease

    acquired = datetime(2026, 8, 25, tzinfo=UTC)
    reads = iter(
        [
            DeployLease("A", 1, 600, None, "rollout", acquired),
            DeployLease("B", 0, 600, None, "rollout", acquired),
        ]
    )
    cleared: list[tuple[object, ...]] = []
    monkeypatch.setattr(ops_cluster, "read_update_lease", lambda: next(reads))
    monkeypatch.setattr(ops_cluster.pause_owner, "mark_paused", lambda *_a: None)
    monkeypatch.setattr(ops_cluster.pause_owner, "clear", lambda *a: cleared.append(a) or True)
    monkeypatch.setattr(
        ops_cluster,
        "pause_local_cluster",
        lambda: pytest.fail("a replaced lease must not authorize pause"),
    )

    with pytest.raises(ClusterUpdateInProgress):
        ops_cluster.cluster_stop_op("A", acquired)
    assert cleared == [("A", acquired)]


@pytest.mark.parametrize("compensation_succeeds", [True, False])
def test_cluster_stop_records_only_a_successful_pause_compensation(
    compensation_succeeds: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import UTC, datetime

    from shared.cluster_lock import DeployLease

    acquired = datetime(2026, 8, 25, tzinfo=UTC)
    lease = DeployLease("A", 1, 600, None, "rollout", acquired)
    resumed: list[tuple[object, ...]] = []
    monkeypatch.setattr(ops_cluster, "read_update_lease", lambda: lease)
    monkeypatch.setattr(ops_cluster.pause_owner, "mark_paused", lambda *_a: None)
    monkeypatch.setattr(
        ops_cluster.pause_owner, "mark_resumed", lambda *a: resumed.append(a) or True
    )
    monkeypatch.setattr(
        ops_cluster, "pause_local_cluster", lambda: (_ for _ in ()).throw(OSError("pause"))
    )
    if compensation_succeeds:
        monkeypatch.setattr(ops_cluster, "unpause_local_cluster", lambda: None)
    else:
        monkeypatch.setattr(
            ops_cluster,
            "unpause_local_cluster",
            lambda: (_ for _ in ()).throw(OSError("unpause")),
        )

    with pytest.raises(OSError, match="pause"):
        ops_cluster.cluster_stop_op("A", acquired)
    assert resumed == ([("A", acquired)] if compensation_succeeds else [])


def test_cluster_resume_op_invokes_unpause(monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import UTC, datetime

    from shared.pause_owner import PauseOwnerSnapshot

    acquired = datetime(2026, 8, 25, tzinfo=UTC)
    called: list[bool] = []
    monkeypatch.setattr(
        ops_cluster.updater_handoff,
        "read",
        lambda: ops_cluster.updater_handoff.UpdaterHandoffSnapshot(status="inactive"),
    )
    monkeypatch.setattr(
        ops_cluster.pause_owner,
        "read",
        lambda: PauseOwnerSnapshot(status="paused", holder="gateway:pid1", acquired_at=acquired),
    )
    monkeypatch.setattr(ops_cluster.pause_owner, "mark_resumed", lambda *_a: True)
    monkeypatch.setattr(ops_cluster, "unpause_local_cluster", lambda: called.append(True))
    monkeypatch.setattr(
        ops_cluster,
        "read_update_lease",
        lambda: pytest.fail("resume must not require the gateway DB"),
    )
    result = ops_cluster.cluster_resume_op("gateway:pid1", acquired)
    assert result == {}
    assert called == [True]


def test_late_resume_cannot_unpause_a_new_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import UTC, datetime, timedelta

    from ops.cluster import ClusterUpdateInProgress
    from shared.pause_owner import PauseOwnerSnapshot

    acquired_a = datetime(2026, 8, 25, tzinfo=UTC)
    acquired_b = acquired_a + timedelta(seconds=1)
    monkeypatch.setattr(
        ops_cluster.updater_handoff,
        "read",
        lambda: ops_cluster.updater_handoff.UpdaterHandoffSnapshot(status="inactive"),
    )
    monkeypatch.setattr(
        ops_cluster.pause_owner,
        "read",
        lambda: PauseOwnerSnapshot(status="paused", holder="same", acquired_at=acquired_b),
    )
    monkeypatch.setattr(
        ops_cluster,
        "unpause_local_cluster",
        lambda: pytest.fail("generation A must not unpause generation B"),
    )
    with pytest.raises(ClusterUpdateInProgress, match="different deploy generation"):
        ops_cluster.cluster_resume_op("same", acquired_a)


def test_stale_deploy_resume_cannot_unpause_a_live_local_updater(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import UTC, datetime

    from ops.cluster import ClusterUpdateInProgress
    from shared.pause_owner import PauseOwnerSnapshot

    acquired = datetime(2026, 8, 25, tzinfo=UTC)
    monkeypatch.setattr(
        ops_cluster.updater_handoff,
        "read",
        lambda: ops_cluster.updater_handoff.UpdaterHandoffSnapshot(
            status="running",
            generation="local-B",
            owner_pid=123,
            owner_create_time=1.0,
        ),
    )
    monkeypatch.setattr(ops_cluster.updater_handoff, "owner_is_live", lambda _s: True)
    monkeypatch.setattr(
        ops_cluster.pause_owner,
        "read",
        lambda: PauseOwnerSnapshot(status="paused", holder="deploy-A", acquired_at=acquired),
    )
    monkeypatch.setattr(
        ops_cluster,
        "unpause_local_cluster",
        lambda: pytest.fail("stale deploy A must not unpause local updater B"),
    )
    with pytest.raises(ClusterUpdateInProgress, match="local updater is running"):
        ops_cluster.cluster_resume_op("deploy-A", acquired)


@pytest.mark.parametrize("boundary", ["invalid-journal", "invalid-handoff", "pending"])
def test_cluster_resume_fails_closed_at_corrupt_or_pending_owner_boundaries(
    boundary: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import UTC, datetime, timedelta

    from ops.cluster import ClusterUpdateInProgress
    from shared.pause_owner import PauseOwnerSnapshot

    acquired = datetime(2026, 8, 25, tzinfo=UTC)
    owner = (
        PauseOwnerSnapshot(status="invalid")
        if boundary == "invalid-journal"
        else PauseOwnerSnapshot(status="paused", holder="A", acquired_at=acquired)
    )
    handoff = (
        ops_cluster.updater_handoff.UpdaterHandoffSnapshot(status="invalid")
        if boundary == "invalid-handoff"
        else ops_cluster.updater_handoff.UpdaterHandoffSnapshot(
            status="pending",
            generation="B",
            expires_at=acquired + timedelta(minutes=1),
            expired=False,
        )
    )
    monkeypatch.setattr(ops_cluster.pause_owner, "read", lambda: owner)
    monkeypatch.setattr(ops_cluster.updater_handoff, "read", lambda: handoff)
    monkeypatch.setattr(
        ops_cluster,
        "unpause_local_cluster",
        lambda: pytest.fail("an unreadable/newer owner must not be unpaused"),
    )

    with pytest.raises(ClusterUpdateInProgress):
        ops_cluster.cluster_resume_op("A", acquired)


def test_retried_completed_resume_is_idempotent_during_a_later_local_update(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import UTC, datetime

    from shared.pause_owner import PauseOwnerSnapshot

    acquired = datetime(2026, 8, 25, tzinfo=UTC)
    monkeypatch.setattr(
        ops_cluster.pause_owner,
        "read",
        lambda: PauseOwnerSnapshot(status="resumed", holder="deploy-A", acquired_at=acquired),
    )
    monkeypatch.setattr(
        ops_cluster.updater_handoff,
        "read",
        lambda: pytest.fail("resumed retry must return before inspecting a later updater"),
    )
    monkeypatch.setattr(
        ops_cluster,
        "unpause_local_cluster",
        lambda: pytest.fail("resumed retry must make no posture write"),
    )
    assert ops_cluster.cluster_resume_op("deploy-A", acquired) == {}


def test_first_adoption_legacy_resume_is_idempotent_without_an_exact_journal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from shared.pause_owner import PauseOwnerSnapshot

    owners = iter(
        [PauseOwnerSnapshot(status="inactive"), PauseOwnerSnapshot(status="legacy-resumed")]
    )
    unpaused: list[bool] = []
    marked: list[bool] = []
    monkeypatch.setattr(ops_cluster.pause_owner, "read", lambda: next(owners))
    monkeypatch.setattr(
        ops_cluster.updater_handoff,
        "read",
        lambda: ops_cluster.updater_handoff.UpdaterHandoffSnapshot(status="inactive"),
    )
    monkeypatch.setattr(ops_cluster, "unpause_local_cluster", lambda: unpaused.append(True))
    monkeypatch.setattr(
        ops_cluster.pause_owner,
        "mark_legacy_resumed",
        lambda: marked.append(True) or PauseOwnerSnapshot(status="legacy-resumed"),
    )

    assert ops_cluster.cluster_resume_legacy_op() == {}
    assert ops_cluster.cluster_resume_legacy_op() == {}
    assert unpaused == [True]
    assert marked == [True]


@pytest.mark.parametrize("owner_status", ["paused", "resumed", "invalid"])
def test_legacy_resume_never_bypasses_an_exact_or_invalid_journal(
    owner_status: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import UTC, datetime

    from ops.cluster import ClusterUpdateInProgress
    from shared.pause_owner import PauseOwnerSnapshot

    exact = owner_status in ("paused", "resumed")
    monkeypatch.setattr(
        ops_cluster.pause_owner,
        "read",
        lambda: PauseOwnerSnapshot(
            status=owner_status,  # type: ignore[arg-type]
            holder="B" if exact else None,
            acquired_at=datetime(2026, 8, 25, tzinfo=UTC) if exact else None,
        ),
    )
    monkeypatch.setattr(
        ops_cluster,
        "unpause_local_cluster",
        lambda: pytest.fail("legacy compatibility must not unpause exact B"),
    )

    with pytest.raises(ClusterUpdateInProgress, match="exact or unreadable"):
        ops_cluster.cluster_resume_legacy_op()


def test_cluster_update_op_returns_session_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    expected = {"session": "ava-updater", "log": "/var/log/updater-123.log"}
    monkeypatch.setattr(ops_cluster, "spawn_update", lambda **_kw: expected)
    assert ops_cluster.cluster_update_op() == expected


def test_cluster_update_op_forwards_restart_only(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}
    monkeypatch.setattr(
        ops_cluster,
        "spawn_update",
        lambda *, restart_only=False, target_sha=None, **_kw: (
            seen.update(ro=restart_only, sha=target_sha) or {}
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
        lambda *, restart_only=False, target_sha=None, **_kw: (
            seen.update(ro=restart_only, sha=target_sha) or {}
        ),
    )
    ops_cluster.cluster_update_op(target_sha="PINNEDSHA")
    assert seen["sha"] == "PINNEDSHA"


def test_cluster_update_op_never_guesses_spawn_failure_compensation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only spawn_update knows definitive-not-started from post-Popen ambiguity.

    The RPC wrapper must propagate the error without unpausing a child that may
    already be running; definitive compensation is locked at the spawn seam.
    """

    def _boom(**_kw: object) -> dict[str, str]:
        raise ValueError("migrations layout broken")

    monkeypatch.setattr(ops_cluster, "spawn_update", _boom)
    called: list[bool] = []
    monkeypatch.setattr(ops_cluster, "unpause_local_cluster", lambda: called.append(True))

    with pytest.raises(ValueError, match="migrations layout broken"):
        ops_cluster.cluster_update_op()
    assert called == []


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
    monkeypatch.setattr(ops_cluster, "spawn_update", lambda **_kw: expected)
    called: list[bool] = []
    monkeypatch.setattr(ops_cluster, "unpause_local_cluster", lambda: called.append(True))

    assert ops_cluster.cluster_update_op() == expected
    assert called == []


def test_cluster_rollout_op_returns_session_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    expected = {"session": "ava-rollout", "log": "/var/log/rollout-123.log"}
    monkeypatch.setattr(ops_cluster, "spawn_rollout", lambda _origin, **_kw: expected)
    assert ops_cluster.cluster_rollout_op("test-origin") == expected


def test_cluster_restart_op_returns_session_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    expected = {"session": "ava-cluster-restart", "log": "/var/log/cluster-restart-123.log"}
    monkeypatch.setattr(ops_cluster, "spawn_restart", lambda _origin, **_kw: expected)
    assert ops_cluster.cluster_restart_op("test-origin") == expected


def test_cluster_update_check_op_returns_check(monkeypatch: pytest.MonkeyPatch) -> None:
    from ops.cluster import UpdateCheck

    chk = UpdateCheck(behind=2, frontend_changed=True, backend_changed=False, needs_replay=False)
    monkeypatch.setattr(ops_cluster, "update_check", lambda: chk)
    assert ops_cluster.cluster_update_check_op() is chk


def test_cluster_status_op_returns_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    from ops.cluster import ClusterStatus

    snap = ClusterStatus(
        machine_name="wsl", serve_gateway=False, serve_agent_runner=True, paused=False
    )
    expected_pool = object()
    seen: list[object] = []

    def _snapshot(pool: object | None = None) -> ClusterStatus:
        assert pool is expected_pool
        seen.append(pool)
        return snap

    monkeypatch.setattr(ops_cluster, "status_snapshot", _snapshot)

    assert ops_cluster.cluster_status_op(expected_pool) is snap
    assert seen == [expected_pool]


@pytest.mark.asyncio
async def test_mark_agent_exited_op_rowcount_gt_one_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """rowcount > 1 means the agents_meta PK (one row per id) is violated —
    fail-loud RuntimeError. The PK makes this impossible in practice, so a
    faked cursor.rowcount=2 is the only way to exercise the guard. (The
    status-respecting WHERE-IN guard itself is covered against a real DB in
    tests/gateway/test_agents_internals.py:TestExitedEndpoint.)"""
    monkeypatch.setattr(ops_exit, "list_open_page_names", lambda _conn, _aid: [])
    fake_cursor = MagicMock()
    fake_cursor.rowcount = 2
    fake_conn = MagicMock()
    fake_conn.cursor.return_value.__enter__.return_value = fake_cursor
    fake_pool = MagicMock()
    fake_pool.connection.return_value.__enter__.return_value = fake_conn
    with pytest.raises(RuntimeError, match="PK invariant violated"):
        await ops_exit.mark_agent_exited_op(42, fake_pool)


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
        monkeypatch.setattr(ops_lifecycle, "get_agent_status", lambda _aid: next(statuses))
        monkeypatch.setattr(ops_lifecycle, "get_agent_machine", lambda _aid: "home-a")
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

        async def _fake_dispatch(*args: object, **kwargs: object) -> dict:
            dispatch_called.append(kwargs)
            raise ops_lifecycle._cluster_rpc.ClusterOpUnreachable("ops server not reachable")

        monkeypatch.setattr(ops_lifecycle._cluster_rpc, "dispatch_to_machine", _fake_dispatch)

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
        monkeypatch.setattr(ops_lifecycle, "get_agent_status", lambda _aid: next(statuses))
        monkeypatch.setattr(ops_lifecycle, "get_agent_machine", lambda _aid: "wsl")
        monkeypatch.setattr(ops_lifecycle, "machine_name", lambda: "gateway-host")

        async def _no_local(*_a: object, **_kw: object) -> ResurrectAgentResponse:
            raise AssertionError("remote-homed resurrect must not launch locally")

        monkeypatch.setattr(ops_lifecycle, "resurrect_agent_op", _no_local)
        captured: dict[str, object] = {}

        async def _fake_dispatch(
            target_machine: str,
            kind: str,
            payload: dict,
            **_kw: object,
        ) -> dict:
            captured.update(target=target_machine, kind=kind, payload=payload)
            return {"status": "spawned"}

        monkeypatch.setattr(ops_lifecycle._cluster_rpc, "dispatch_to_machine", _fake_dispatch)

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

        monkeypatch.setattr(ops_lifecycle, "get_agent_status", lambda _aid: AgentStatus.TERMINATED)
        monkeypatch.setattr(ops_lifecycle, "get_agent_machine", lambda _aid: "wsl")
        monkeypatch.setattr(ops_lifecycle, "machine_name", lambda: "gateway-host")

        async def _unreachable(*_a: object, **_kw: object) -> dict:
            raise ClusterOpUnreachable("ops server for machine='wsl' unreachable")

        monkeypatch.setattr(ops_lifecycle._cluster_rpc, "dispatch_to_machine", _unreachable)

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

        monkeypatch.setattr(ops_lifecycle, "get_agent_status", lambda _aid: AgentStatus.TERMINATED)
        monkeypatch.setattr(ops_lifecycle, "get_agent_machine", lambda _aid: "wsl")
        monkeypatch.setattr(ops_lifecycle, "machine_name", lambda: "gateway-host")

        async def _failed(*_a: object, **_kw: object) -> dict:
            raise ClusterOpFailed({"error": "launch failed on the home machine"})

        monkeypatch.setattr(ops_lifecycle._cluster_rpc, "dispatch_to_machine", _failed)

        status = await ops_lifecycle.resurrect_if_terminated(
            7, trigger_inbound_id=99, trigger_inbound_kind="chat"
        )
        assert status is AgentStatus.TERMINATED

    @pytest.mark.asyncio
    async def test_not_terminated_short_circuits(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from shared.agents import AgentStatus

        monkeypatch.setattr(ops_lifecycle, "get_agent_status", lambda _aid: AgentStatus.RUNNING)

        def _no_machine_read(_aid: int) -> str:
            raise AssertionError("a live agent must not trigger a machine lookup")

        monkeypatch.setattr(ops_lifecycle, "get_agent_machine", _no_machine_read)

        status = await ops_lifecycle.resurrect_if_terminated(
            5, trigger_inbound_id=99, trigger_inbound_kind="chat"
        )
        assert status is AgentStatus.RUNNING


@pytest.mark.asyncio
async def test_launch_agent_op_hosted_skips_process_and_wakes(
    monkeypatch: pytest.MonkeyPatch, stub_pool: object
) -> None:
    """Hosted mode: the row the gateway created IS the agent. No fork, no
    launch-confirm — the prompt INSERT (which publishes its own wake inside
    `insert_inbound_message`) plus one explicit wake is the whole launch."""
    monkeypatch.setattr(ops_launch.runner_mode, "is_hosted", lambda: True)
    launches: list[int] = []
    confirmed: list[int] = []
    monkeypatch.setattr(
        ops_launch.agent_launch,
        "_launch_agent_process",
        lambda *_a, **_k: launches.append(1),
    )
    monkeypatch.setattr(
        ops_launch.agent_launch,
        "schedule_launch_confirm",
        lambda agent_id, _attempt: confirmed.append(agent_id),
    )
    inserted: list[tuple[int, str, str]] = []

    def _fake_insert(_pool: object, agent_id: int, prompt: str, source: str) -> int:
        inserted.append((agent_id, prompt, source))
        return 11

    monkeypatch.setattr(ops_launch, "_insert_prompt_blocking", _fake_insert)

    async def _fake_publish(*_a: object, **_k: object) -> None:
        return None

    monkeypatch.setattr(ops_lifecycle, "publish_inbound_arrived", _fake_publish)
    wakes: list[tuple[int, str]] = []
    monkeypatch.setattr(
        ops_launch, "publish_inbound_wake", lambda aid, payload: wakes.append((aid, payload))
    )

    body = LaunchAgentRequest(agent_id=7, prompt="go do X", prompt_source="user")
    result = await ops_lifecycle.launch_agent_op(body, stub_pool)  # type: ignore[arg-type]
    assert result.id == 7
    assert launches == []  # hosted never forks
    assert confirmed == []  # hosted never confirms a pid
    assert inserted == [(7, "go do X", "user")]
    assert wakes == [(7, "0")]


@pytest.mark.asyncio
async def test_launch_agent_op_hosted_fork_still_wakes(
    monkeypatch: pytest.MonkeyPatch, stub_pool: object
) -> None:
    """A fork's inbounds were pre-inserted by create_agent_row as raw SQL (no
    wake inside) — the hosted launch must publish the wake explicitly, and must
    not insert a second prompt."""
    monkeypatch.setattr(ops_launch.runner_mode, "is_hosted", lambda: True)
    monkeypatch.setattr(ops_launch.agent_launch, "_launch_agent_process", lambda *_a, **_k: None)
    monkeypatch.setattr(
        ops_launch.agent_launch, "schedule_launch_confirm", lambda _id, _attempt=None: None
    )
    inserted: list[int] = []

    def _fake_insert(_pool: object, _agent_id: int, _prompt: str, _source: str) -> int:
        inserted.append(1)
        return 0

    monkeypatch.setattr(ops_launch, "_insert_prompt_blocking", _fake_insert)

    async def _fake_publish(*_a: object, **_k: object) -> None:
        return None

    monkeypatch.setattr(ops_lifecycle, "publish_inbound_arrived", _fake_publish)
    wakes: list[tuple[int, str]] = []
    monkeypatch.setattr(
        ops_launch, "publish_inbound_wake", lambda aid, payload: wakes.append((aid, payload))
    )

    body = LaunchAgentRequest(agent_id=8)
    result = await ops_lifecycle.launch_agent_op(body, stub_pool)  # type: ignore[arg-type]
    assert result.id == 8
    assert inserted == []  # fork prompt is delivered pre-launch, never here
    assert wakes == [(8, "0")]


async def test_force_terminate_hosted_skips_process_kill_and_cancels_turn(
    monkeypatch: pytest.MonkeyPatch, stub_pool: object
) -> None:
    """Hosted force-terminate: no process to SIGKILL — the DB fence runs with
    kill_process=False and the turn-cancel acceleration fires after the
    transaction. The durable terminate inbound inserted by the fence is the
    correctness mechanism; the cancel only accelerates a wedged turn."""
    from shared.agents import AgentStatus

    monkeypatch.setattr(ops_lifecycle.runner_mode, "is_hosted", lambda: True)
    captured: dict[str, object] = {}

    def _fake_force_blocking(
        aid: int, _body: object, _pool: object, *, kill_process: bool
    ) -> tuple[AgentStatus, int | None, list[str], int]:
        captured["agent_id"] = aid
        captured["kill_process"] = kill_process
        return AgentStatus.RUNNING, None, [], 91

    monkeypatch.setattr(ops_lifecycle, "_terminate_force_blocking", _fake_force_blocking)
    cancelled: list[tuple[int, int]] = []

    async def _fake_cancel(aid: int, command_id: int) -> None:
        cancelled.append((aid, command_id))

    monkeypatch.setattr(ops_lifecycle, "_cancel_hosted_turn_best_effort", _fake_cancel)

    async def _fake_page_closed(*_a: object, **_k: object) -> None:
        return None

    monkeypatch.setattr(ops_lifecycle, "publish_page_closed", _fake_page_closed)

    resp = await ops_lifecycle.terminate_agent_op(
        9,
        TerminateAgentRequest(force=True),
        stub_pool,  # type: ignore[arg-type]
    )
    assert resp.status == "enqueued"
    assert captured == {"agent_id": 9, "kill_process": False}
    assert cancelled == [(9, 91)]


@pytest.mark.asyncio
async def test_force_terminate_process_mode_still_kills_the_process(
    monkeypatch: pytest.MonkeyPatch, stub_pool: object
) -> None:
    """The regression guard: process mode keeps kill_process=True (the session
    kill + SIGKILL path) and never calls the hosted turn-cancel acceleration."""
    from shared.agents import AgentStatus

    monkeypatch.setattr(ops_lifecycle.runner_mode, "is_hosted", lambda: False)
    captured: dict[str, object] = {}

    def _fake_force_blocking(
        aid: int, _body: object, _pool: object, *, kill_process: bool
    ) -> tuple[AgentStatus, int | None, list[str], int]:
        captured["agent_id"] = aid
        captured["kill_process"] = kill_process
        return AgentStatus.RUNNING, 1234, [], 91

    monkeypatch.setattr(ops_lifecycle, "_terminate_force_blocking", _fake_force_blocking)

    async def _fake_cancel(_aid: int, _command_id: int) -> None:
        raise AssertionError("process mode must not call the hosted cancel")

    monkeypatch.setattr(ops_lifecycle, "_cancel_hosted_turn_best_effort", _fake_cancel)

    async def _fake_page_closed(*_a: object, **_k: object) -> None:
        return None

    monkeypatch.setattr(ops_lifecycle, "publish_page_closed", _fake_page_closed)

    resp = await ops_lifecycle.terminate_agent_op(
        9,
        TerminateAgentRequest(force=True),
        stub_pool,  # type: ignore[arg-type]
    )
    assert resp.status == "force_killed"
    assert captured == {"agent_id": 9, "kill_process": True}


@pytest.mark.asyncio
async def test_launch_agent_op_hosted_failure_reclaims_its_row(
    monkeypatch: pytest.MonkeyPatch, stub_pool: object
) -> None:
    """Hosted mode has no unclaimed-idling reaper (the restarter is retired), so
    a failed hosted launch must reclaim its own corpse: any failure after the
    row exists marks it terminated ('launch-confirm', the same class the
    process-mode launch confirm stamps) and re-raises."""
    monkeypatch.setattr(ops_launch.runner_mode, "is_hosted", lambda: True)
    monkeypatch.setattr(ops_launch.agent_launch, "_launch_agent_process", lambda *_a, **_k: None)

    def _boom(_pool: object, _agent_id: int, _prompt: str, _source: str) -> int:
        raise RuntimeError("prompt insert failed")

    monkeypatch.setattr(ops_launch, "_insert_prompt_blocking", _boom)
    reclaimed: list[tuple[int, str]] = []

    def _fake_reclaim(agent_id: int, _pool: object, *, source: str) -> list[str]:
        reclaimed.append((agent_id, source))
        return []

    monkeypatch.setattr(ops_lifecycle, "_force_mark_terminated", _fake_reclaim)

    body = LaunchAgentRequest(agent_id=7, prompt="go", prompt_source="user")
    with pytest.raises(RuntimeError, match="prompt insert failed"):
        await ops_lifecycle.launch_agent_op(body, stub_pool)  # type: ignore[arg-type]
    assert reclaimed == [(7, "launch-confirm")]


@pytest.mark.asyncio
async def test_launch_agent_op_hosted_validation_failure_reclaims_its_row(
    monkeypatch: pytest.MonkeyPatch, stub_pool: object
) -> None:
    """A hosted row exists before validation runs, so a validation failure
    must land inside the reclaim too: leaked outside it, the idling row has
    no restarter reaper and the heartbeat pokes it into a prompt-less zombie
    (QA #1029 required fix)."""
    monkeypatch.setattr(ops_launch.runner_mode, "is_hosted", lambda: True)

    def _boom_validate(*_a: object, **_k: object) -> None:
        raise RuntimeError("bad model config")

    monkeypatch.setattr("shared.lm.factory.validate_model_config", _boom_validate)
    reclaimed: list[tuple[int, str]] = []

    def _fake_reclaim(agent_id: int, _pool: object, *, source: str) -> list[str]:
        reclaimed.append((agent_id, source))
        return []

    monkeypatch.setattr(ops_lifecycle, "_force_mark_terminated", _fake_reclaim)

    body = LaunchAgentRequest(agent_id=7, prompt="go", prompt_source="user")
    with pytest.raises(RuntimeError, match="bad model config"):
        await ops_lifecycle.launch_agent_op(body, stub_pool)  # type: ignore[arg-type]
    assert reclaimed == [(7, "launch-confirm")]
