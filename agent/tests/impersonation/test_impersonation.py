"""Takeover barriers: consent, resource closure, checkpoint ordering and replay."""

from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, cast
from unittest.mock import ANY, AsyncMock, MagicMock, Mock
from uuid import uuid4

import psycopg
import pytest
from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel, Field

from agent import impersonation
from agent.graph.exec._result import lifecycle_exception_from_name
from agent.graph.exec.protocol import read_request, write_request
from agent.state import BaseAgentState
from agent.tests._fakes import placeholder_runtime
from base.agents.context import AvaContext
from base.agents.lifecycle import AgentImpersonation
from base.agents.observation.relay_supervision import RelayChild, RelaySupervision
from base.db import Database
from base.events.live.bus import EventBus
from base.native_process.runtime_incarnation import RuntimeIncarnation
from base.native_process.turn_identity import HostedTurnResources
from tests.fixtures.pin_agent import exec_context, pin_agent


@pytest.fixture
def relays() -> RelaySupervision:
    return RelaySupervision()


@pytest.fixture
def gate_ctx(
    database: Database,
    event_bus: EventBus,
    relays: RelaySupervision,
) -> AvaContext:
    return AvaContext(db=database, bus=event_bus, relays=relays)


@pytest.fixture
def native_gate_ctx(gate_ctx: AvaContext, incarnation: RuntimeIncarnation) -> AvaContext:
    return replace(gate_ctx, original_incarnation=incarnation)


@pytest.fixture
def incarnation() -> Iterator[RuntimeIncarnation]:
    token = RuntimeIncarnation(42, uuid4(), uuid4())
    yield token


def _session(status: str = "active", **values: Any) -> dict[str, Any]:
    return {
        "id": "lease-1",
        "automatic": False,
        "handoff_applied_at": None,
        "process_metadata": {},
        "source": "external_agent:codex:task1",
        "status": status,
        "reason": "Finish the assigned task",
        "consent_version": 1,
        "plugin_delta": [],
        "delta_version": 0,
        "applied_version": 0,
        "relay_provider": "codex",
        "relay_thread_id": "thread-1",
        "relay_codex_remote": None,
        "relay_heartbeat_at": datetime.now(UTC),
        "relay_last_failure_at": None,
        "relay_generation": 1,
        "relay_identity": None,
        "relay_minted_at": None,
        **values,
    }


async def test_consent_carries_actual_source_and_survives_compaction(
    monkeypatch: pytest.MonkeyPatch,
    gate_ctx: AvaContext,
) -> None:
    monkeypatch.setattr(
        impersonation, "native_status", AsyncMock(return_value=_session("requested"))
    )
    first = await impersonation.claim_gate(BaseAgentState(), 42, gate_ctx)
    assert first is not None
    assert first.goto == "before_llm"
    message = cast(HumanMessage, cast(dict[str, Any], first.update)["messages"][0])
    assert message.id == "impersonation-request:lease-1:1"
    content = cast(str, message.content)  # pyright: ignore[reportUnknownMemberType]
    assert "External agent" in content and "codex" in content
    assert "ava.impersonation.accept('lease-1', start_message=" in content
    compacted = BaseAgentState(impersonation_request_id="lease-1:1")
    assert await impersonation.claim_gate(compacted, 42, gate_ctx) is None


@pytest.mark.parametrize("status", ["accepted", "active", "released", "expired"])
async def test_hold_ends_before_claim_or_compaction(
    monkeypatch: pytest.MonkeyPatch, status: str, gate_ctx: AvaContext
) -> None:
    monkeypatch.setattr(impersonation, "native_status", AsyncMock(return_value=_session(status)))
    decision = await impersonation.claim_gate(BaseAgentState(), 42, gate_ctx)
    assert decision is not None
    assert decision.goto == END
    hook = AsyncMock(return_value=Command(goto="llm"))
    guarded = impersonation.protect_native_hooks(hook)
    result = await guarded(
        BaseAgentState(),
        placeholder_runtime(),
        {"configurable": {"thread_id": "42"}},
    )
    assert result.goto == END
    hook.assert_not_awaited()


async def test_activation_waits_for_resource_closure(
    monkeypatch: pytest.MonkeyPatch,
    incarnation: RuntimeIncarnation,
    database: Database,
    event_bus: EventBus,
    relays: RelaySupervision,
) -> None:
    monkeypatch.setattr(
        impersonation, "native_status", AsyncMock(return_value=_session("accepted"))
    )
    activate = Mock(return_value=_session())
    monkeypatch.setattr("base.agents.impersonation.activate", activate)
    resources = HostedTurnResources(unresolved={Path("request"): object()})
    with pytest.raises(RuntimeError, match="unresolved native exec"):
        await impersonation.settle_checkpoint(
            MagicMock(),
            database,
            event_bus,
            42,
            relays,
            incarnation=incarnation,
            resources=resources,
        )
    activate.assert_not_called()
    assert not await impersonation.settle_checkpoint(
        MagicMock(),
        database,
        event_bus,
        42,
        relays,
        activate_accepted=False,
        incarnation=incarnation,
        resources=resources,
    )
    activate.assert_not_called()


async def test_checkpoint_receipt_prevents_reapplying_non_idempotent_delta(
    monkeypatch: pytest.MonkeyPatch,
    incarnation: RuntimeIncarnation,
    database: Database,
    event_bus: EventBus,
    relays: RelaySupervision,
) -> None:
    def add(left: int, right: int) -> int:
        return left + right

    class State(BaseModel):
        counter: Annotated[int, add] = 0
        impersonation_applied: dict[str, object] = Field(default_factory=dict)

    builder = StateGraph(State)

    def idle(_state: State) -> dict[str, Any]:
        return {}

    builder.add_node("idle", idle)  # type: ignore[arg-type]
    builder.add_edge(START, "idle")
    builder.add_edge("idle", END)
    graph: Any = builder.compile(checkpointer=MemorySaver())  # pyright: ignore[reportUnknownMemberType]
    config: RunnableConfig = {"configurable": {"thread_id": "42"}}
    await graph.ainvoke({"counter": 0}, config)
    session = _session("released", plugin_delta=[{"counter": 3}], delta_version=1)
    monkeypatch.setattr(impersonation, "native_status", AsyncMock(return_value=session))

    def decode(delta: dict[str, Any], state_cls: type[Any]) -> dict[str, Any]:
        assert state_cls is State  # the class the graph runs on
        return delta

    monkeypatch.setattr("ava.external.state.decode_plugin_delta", decode)
    receipt = Mock(side_effect=RuntimeError("receipt commit lost"))
    monkeypatch.setattr("base.agents.impersonation.mark_plugin_applied", receipt)
    with pytest.raises(RuntimeError, match="receipt commit lost"):
        await impersonation.settle_checkpoint(
            graph, database, event_bus, 42, relays, incarnation=incarnation, resources=None
        )
    assert (await graph.aget_state(config)).values["counter"] == 3
    receipt.side_effect = None
    await impersonation.settle_checkpoint(
        graph, database, event_bus, 42, relays, incarnation=incarnation, resources=None
    )
    assert (await graph.aget_state(config)).values["counter"] == 3
    assert receipt.call_count == 2


def test_accept_stops_exec_and_uses_captured_incarnation(
    monkeypatch: pytest.MonkeyPatch,
    incarnation: RuntimeIncarnation,
) -> None:
    from ava.impersonation import accept

    accepted = Mock()
    monkeypatch.setattr("base.agents.impersonation.accept", accepted)
    pin_agent(incarnation.agent_id, incarnation=incarnation)
    with pytest.raises(AgentImpersonation):
        accept("lease-1", "Hand the task to the external session.")
    accepted.assert_called_once_with(
        ANY, ANY, "lease-1", 42, incarnation, "Hand the task to the external session."
    )
    assert isinstance(lifecycle_exception_from_name("AgentImpersonation"), AgentImpersonation)


def test_exec_envelope_carries_parent_incarnation(
    tmp_path: Path, incarnation: RuntimeIncarnation
) -> None:
    path = tmp_path / "request.json"
    write_request(
        path,
        code="pass",
        context=exec_context(42).describe(),
        timeout_s=10,
        state={},
        incarnation=incarnation,
    )
    assert read_request(path).incarnation == incarnation


async def test_control_claim_leaves_cancel_for_external_or_resumed_native(
    db_conn: psycopg.Connection[Any], aops_pool: AsyncConnectionPool[Any]
) -> None:
    from agent.db import claim_inbound_batch
    from tests.fixtures.units import spawn_agent

    agent_id = spawn_agent()
    for kind in ("chat", "compact_request", "cancel"):
        db_conn.execute(
            "INSERT INTO inbound_messages(agent_id,content,kind,source) VALUES(%s,'wait',%s,'user')",
            (agent_id, kind),
        )
    db_conn.commit()
    batch = await claim_inbound_batch(
        aops_pool, agent_id, lifecycle_only=True, incarnation=None, work=None
    )
    assert batch == []
    assert db_conn.execute(
        "SELECT kind FROM inbound_messages WHERE agent_id=%s AND status='pending' ORDER BY kind",
        (agent_id,),
    ).fetchall() == [("cancel",), ("chat",), ("compact_request",)]
    db_conn.commit()
    assert not await impersonation.lifecycle_ready(aops_pool, agent_id)
    # If the external holder never acknowledges cancellation, the ordinary
    # native claim after release/expiry still receives the durable request.
    resumed = await claim_inbound_batch(aops_pool, agent_id, incarnation=None, work=None)
    assert {item.kind for item in resumed} == {"cancel", "chat", "compact_request"}
    assert db_conn.execute(
        "SELECT status FROM inbound_messages WHERE agent_id=%s AND kind='cancel'",
        (agent_id,),
    ).fetchone() == ("done",)


@pytest.mark.parametrize("kind", ["restart", "terminate"])
async def test_control_claim_records_superseded_accepted_intent(
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    kind: str,
    database: Database,
) -> None:

    from agent.db import claim_inbound_batch
    from agent.ownership.hosted import admit_hosted_runtime
    from base.cluster.machine import machine_name
    from tests.fixtures.units import spawn_agent

    agent_id = spawn_agent()
    owner = await admit_hosted_runtime(
        aops_pool,
        agent_id,
        machine_name(),
        uuid4(),
        expected_from="idling",
        db=database,
    )
    assert owner is not None
    db_conn.execute(
        "INSERT INTO inbound_messages(agent_id,content,kind,source) VALUES(%s,'',%s,'user')",
        (agent_id, kind),
    )
    db_conn.commit()
    accepted = await claim_inbound_batch(
        aops_pool, agent_id, lifecycle_only=True, incarnation=owner, work=None
    )
    assert len(accepted) == 1 and accepted[0].durable_lifecycle
    replacement = RuntimeIncarnation(agent_id, uuid4(), uuid4())
    db_conn.execute(
        "UPDATE agents_meta SET runtime_generation=%s,runtime_owner=%s WHERE id=%s",
        (replacement.generation, replacement.owner, agent_id),
    )
    db_conn.commit()
    assert (
        await claim_inbound_batch(
            aops_pool, agent_id, lifecycle_only=True, incarnation=replacement, work=None
        )
        == []
    )
    assert db_conn.execute(
        "SELECT status,applied_at,target_generation,target_owner,payload->'lifecycle_result' "
        "FROM inbound_messages WHERE agent_id=%s AND kind=%s",
        (agent_id, kind),
    ).fetchone() == (
        "done",
        None,
        owner.generation,
        owner.owner,
        {"outcome": "superseded", "reason": "target_replaced"},
    )
    assert db_conn.execute(
        "SELECT lifecycle_command_id FROM agents_meta WHERE id=%s", (agent_id,)
    ).fetchone() == (None,)


@pytest.mark.parametrize("kind", ["restart", "terminate"])
async def test_control_claim_preserves_unaccepted_intent(
    db_conn: psycopg.Connection[Any], aops_pool: AsyncConnectionPool[Any], kind: str
) -> None:
    from agent.db import claim_inbound_batch
    from tests.fixtures.units import spawn_agent

    agent_id = spawn_agent()
    db_conn.execute(
        "INSERT INTO inbound_messages(agent_id,content,kind,source) VALUES(%s,'',%s,'user')",
        (agent_id, kind),
    )
    db_conn.commit()
    with pytest.raises(RuntimeError, match="lifecycle claim requires an admitted"):
        await claim_inbound_batch(
            aops_pool, agent_id, lifecycle_only=True, incarnation=None, work=None
        )
    assert db_conn.execute(
        "SELECT status,claimed_at,payload->'lifecycle_result' "
        "FROM inbound_messages WHERE agent_id=%s",
        (agent_id,),
    ).fetchone() == ("pending", None, None)


# ── Relay establishment gate and supervision ────────────────────────────────


def _relay_session(
    status: str = "accepted", *, provider: str = "codex", **values: Any
) -> dict[str, Any]:
    base: dict[str, Any] = {
        "relay_provider": provider,
        "relay_thread_id": "thread-1" if provider == "codex" else None,
        "relay_codex_remote": None,
        "relay_heartbeat_at": None,
        "relay_last_failure_at": None,
        "relay_generation": 1,
        "relay_identity": None,
        "relay_minted_at": None,
    }
    base.update(values)
    return _session(status, **base)


async def test_settle_checkpoint_rolls_back_when_relay_establishment_fails(
    monkeypatch: pytest.MonkeyPatch,
    incarnation: RuntimeIncarnation,
    database: Database,
    event_bus: EventBus,
    relays: RelaySupervision,
) -> None:
    monkeypatch.setattr(
        impersonation, "native_status", AsyncMock(return_value=_relay_session("accepted"))
    )
    activate = Mock()
    monkeypatch.setattr("base.agents.impersonation.activate", activate)
    checkpoint = AsyncMock()
    monkeypatch.setattr("agent.impersonation_handoff.ensure_start_marker", checkpoint)

    def refused(*_args: object) -> bool:
        return False

    monkeypatch.setattr(impersonation, "establish_relay", refused)
    assert not await impersonation.settle_checkpoint(
        MagicMock(), database, event_bus, 42, relays, incarnation=incarnation, resources=None
    )
    activate.assert_not_called()
    checkpoint.assert_awaited_once()


async def test_settle_checkpoint_activates_only_after_relay_ready(
    monkeypatch: pytest.MonkeyPatch,
    incarnation: RuntimeIncarnation,
    database: Database,
    event_bus: EventBus,
    relays: RelaySupervision,
) -> None:
    monkeypatch.setattr(
        impersonation, "native_status", AsyncMock(return_value=_relay_session("accepted"))
    )
    activate = Mock(return_value=_relay_session("active"))
    monkeypatch.setattr("base.agents.impersonation.activate", activate)
    checkpoint = AsyncMock()
    monkeypatch.setattr("agent.impersonation_handoff.ensure_start_marker", checkpoint)
    establish = Mock(return_value=True)
    monkeypatch.setattr(impersonation, "establish_relay", establish)
    assert await impersonation.settle_checkpoint(
        MagicMock(), database, event_bus, 42, relays, incarnation=incarnation, resources=None
    )
    checkpoint.assert_awaited_once()
    establish.assert_called_once()
    activate.assert_called_once()


@pytest.mark.parametrize("provider", ["claude", "dsh"])
def test_establish_relay_session_relay_requires_a_fresh_heartbeat(
    monkeypatch: pytest.MonkeyPatch,
    incarnation: RuntimeIncarnation,
    provider: str,
    database: Database,
    event_bus: EventBus,
    relays: RelaySupervision,
) -> None:
    from datetime import UTC, datetime, timedelta

    fail = Mock()
    monkeypatch.setattr("base.agents.impersonation.fail_acceptance", fail)
    stale = _relay_session(
        "accepted", provider=provider, relay_heartbeat_at=datetime.now(UTC) - timedelta(minutes=5)
    )
    assert impersonation.establish_relay(database, event_bus, stale, incarnation, relays) is False
    fail.assert_called_once()
    fail.reset_mock()
    fresh = _relay_session("accepted", provider=provider, relay_heartbeat_at=datetime.now(UTC))
    assert impersonation.establish_relay(database, event_bus, fresh, incarnation, relays) is True
    fail.assert_not_called()


def test_establish_relay_codex_provisions_spawns_and_waits_for_heartbeat(
    monkeypatch: pytest.MonkeyPatch,
    incarnation: RuntimeIncarnation,
    database: Database,
    event_bus: EventBus,
    relays: RelaySupervision,
) -> None:
    from datetime import UTC, datetime

    provision = Mock(return_value={"relay_generation": 1})
    monkeypatch.setattr("base.agents.impersonation.provision_relay", provision)
    spawn = Mock(return_value=MagicMock(poll=Mock(return_value=None)))
    monkeypatch.setattr(impersonation, "_spawn_codex_relay", spawn)
    ready = {"status": "accepted", "relay_heartbeat_at": datetime.now(UTC)}
    monkeypatch.setattr("base.agents.impersonation.relay_get", Mock(return_value=ready))
    assert (
        impersonation.establish_relay(database, event_bus, _relay_session(), incarnation, relays)
        is True
    )
    provision.assert_called_once()
    provision_token = provision.call_args.args[3]
    assert spawn.call_count == 1
    assert spawn.call_args.args[:6] == (42, "lease-1", provision_token, "thread-1", None, None)
    assert callable(spawn.call_args.args[6])
    assert relays.children[42].token == provision_token


def test_establish_relay_codex_spawn_exit_rolls_back(
    monkeypatch: pytest.MonkeyPatch,
    incarnation: RuntimeIncarnation,
    database: Database,
    event_bus: EventBus,
    relays: RelaySupervision,
) -> None:
    child = MagicMock()
    child.poll.return_value = 5
    child.returncode = 5
    monkeypatch.setattr(
        "base.agents.impersonation.provision_relay", Mock(return_value={"relay_generation": 1})
    )
    monkeypatch.setattr(impersonation, "_spawn_codex_relay", Mock(return_value=child))
    fail = Mock()
    monkeypatch.setattr("base.agents.impersonation.fail_acceptance", fail)
    assert (
        impersonation.establish_relay(database, event_bus, _relay_session(), incarnation, relays)
        is False
    )
    fail.assert_called_once()
    assert "exited during startup" in fail.call_args.args[4]
    assert not relays.children


def test_establish_relay_codex_readiness_timeout_rolls_back(
    monkeypatch: pytest.MonkeyPatch,
    incarnation: RuntimeIncarnation,
    database: Database,
    event_bus: EventBus,
    relays: RelaySupervision,
) -> None:
    child = MagicMock()
    child.poll.return_value = None
    monkeypatch.setattr(
        "base.agents.impersonation.provision_relay", Mock(return_value={"relay_generation": 1})
    )
    monkeypatch.setattr(impersonation, "_spawn_codex_relay", Mock(return_value=child))
    terminate = Mock()
    monkeypatch.setattr(impersonation, "_terminate_relay", terminate)
    fail = Mock()
    monkeypatch.setattr("base.agents.impersonation.fail_acceptance", fail)
    monkeypatch.setattr(impersonation, "_RELAY_READY_TIMEOUT_S", 0.0)
    assert (
        impersonation.establish_relay(database, event_bus, _relay_session(), incarnation, relays)
        is False
    )
    terminate.assert_called_once()
    fail.assert_called_once()
    assert "did not become ready" in fail.call_args.args[4]
    assert not relays.children


async def test_missing_snapshot_cannot_kill_a_replacement_relay(
    monkeypatch: pytest.MonkeyPatch,
    gate_ctx: AvaContext,
) -> None:
    monkeypatch.setattr(impersonation, "native_status", AsyncMock(return_value=None))
    process = MagicMock()
    gate_ctx.relays.children[42] = RelayChild(
        "00000000-0000-0000-0000-000000000001", process, "token", 0.0, 1
    )
    conn = MagicMock()
    conn.execute.return_value.fetchone.return_value = ("active",)
    db = MagicMock()
    db.connect.return_value.__enter__.return_value = conn
    terminate = Mock()
    monkeypatch.setattr(impersonation, "_terminate_relay", terminate)
    await impersonation.supervise_relay(
        db, MagicMock(), None, 42, gate_ctx.relays, incarnation=None
    )
    terminate.assert_not_called()
    assert gate_ctx.relays.children[42].process is process


@pytest.mark.parametrize("same_lease", [False, True])
async def test_delayed_snapshot_cannot_retire_replacement_sender(
    monkeypatch: pytest.MonkeyPatch,
    gate_ctx: AvaContext,
    same_lease: bool,
) -> None:
    session = _relay_session("active", relay_generation=1)
    process = MagicMock()
    gate_ctx.relays.children[42] = RelayChild(
        "lease-1" if same_lease else "lease-2", process, "new-token", 0.0, 2
    )
    terminate = Mock()
    monkeypatch.setattr(impersonation, "_terminate_relay", terminate)
    await impersonation.supervise_relay(
        MagicMock(), MagicMock(), session, 42, gate_ctx.relays, incarnation=None
    )
    terminate.assert_not_called()
    assert gate_ctx.relays.children[42].process is process


@pytest.mark.parametrize("owned", [False, True])
@pytest.mark.parametrize("running", [False, True])
async def test_claim_gate_recovers_stale_delivery_and_keeps_native_parked(
    monkeypatch: pytest.MonkeyPatch,
    incarnation: RuntimeIncarnation,
    native_gate_ctx: AvaContext,
    owned: bool,
    running: bool,
) -> None:
    from datetime import timedelta

    from agent.nodes import END

    session = _relay_session("active", relay_heartbeat_at=datetime.now(UTC) - timedelta(minutes=5))
    monkeypatch.setattr(impersonation, "native_status", AsyncMock(return_value=session))
    monkeypatch.setattr(impersonation, "_provider_anchor_states", Mock(return_value=["alive"]))
    old = MagicMock()
    old.poll.return_value = None if running else 1
    if owned:
        native_gate_ctx.relays.children[42] = RelayChild("lease-1", old, "token", 0.0, 1)
    retired = Mock(return_value=True)
    monkeypatch.setattr(impersonation, "_retire_recorded_relay", retired)
    terminated = Mock()
    monkeypatch.setattr(impersonation, "_terminate_relay", terminated)
    provision = Mock(return_value={"relay_generation": 2})
    monkeypatch.setattr("base.agents.impersonation.provision_relay", provision)
    new = MagicMock()
    spawn = Mock(return_value=new)
    monkeypatch.setattr(impersonation, "_spawn_codex_relay", spawn)
    abort = Mock()
    monkeypatch.setattr("base.agents.impersonation.abort_lease", abort)
    decision = await impersonation.claim_gate(BaseAgentState(), 42, native_gate_ctx)
    assert decision is not None and decision.goto == END
    assert provision.call_args.kwargs == {"expected_generation": 1}
    assert native_gate_ctx.relays.children[42].process is new
    assert terminated.call_count == int(owned)
    assert retired.call_count == int(not owned)
    spawn.assert_called_once()
    abort.assert_not_called()


async def test_startup_grace_does_not_retire_a_fresh_child(
    monkeypatch: pytest.MonkeyPatch,
    incarnation: RuntimeIncarnation,
    native_gate_ctx: AvaContext,
) -> None:
    session = _relay_session("active")
    child = MagicMock()
    child.poll.return_value = None
    native_gate_ctx.relays.children[42] = RelayChild(
        "lease-1", child, "token", impersonation.time.monotonic()
    )
    monkeypatch.setattr(impersonation, "native_status", AsyncMock(return_value=session))
    provision = Mock(return_value={"relay_generation": 1})
    monkeypatch.setattr("base.agents.impersonation.provision_relay", provision)
    await impersonation.claim_gate(BaseAgentState(), 42, native_gate_ctx)
    provision.assert_not_called()
    assert native_gate_ctx.relays.children[42].process is child


async def test_confirmed_child_exit_bypasses_fresh_heartbeat_and_startup_grace(
    monkeypatch: pytest.MonkeyPatch,
    incarnation: RuntimeIncarnation,
    native_gate_ctx: AvaContext,
) -> None:
    session = _relay_session(
        "active", relay_heartbeat_at=datetime.now(UTC), relay_minted_at=datetime.now(UTC)
    )
    old = MagicMock()
    old.poll.return_value = 1
    native_gate_ctx.relays.children[42] = RelayChild(
        "lease-1", old, "old-token", impersonation.time.monotonic(), 1
    )
    monkeypatch.setattr(impersonation, "native_status", AsyncMock(return_value=session))
    monkeypatch.setattr(impersonation, "_provider_anchor_states", Mock(return_value=["alive"]))
    provision = Mock(return_value={"relay_generation": 2})
    monkeypatch.setattr("base.agents.impersonation.provision_relay", provision)
    new = MagicMock()
    spawn = Mock(return_value=new)
    monkeypatch.setattr(impersonation, "_spawn_codex_relay", spawn)
    decision = await impersonation.claim_gate(BaseAgentState(), 42, native_gate_ctx)
    assert decision is not None and decision.goto == END
    assert provision.call_args.kwargs == {"expected_generation": 1}
    assert native_gate_ctx.relays.children[42].process is new
    spawn.assert_called_once()
    old.terminate.assert_not_called()
    assert session["relay_generation"] == 1


async def test_stale_session_relay_retains_authority_with_visible_unsupported_recovery(
    monkeypatch: pytest.MonkeyPatch,
    incarnation: RuntimeIncarnation,
    native_gate_ctx: AvaContext,
) -> None:
    session = _relay_session("active", provider="claude")
    monkeypatch.setattr(impersonation, "_provider_anchor_states", Mock(return_value=["alive"]))
    monkeypatch.setattr(impersonation, "native_status", AsyncMock(return_value=session))
    degradation = Mock()
    monkeypatch.setattr("base.agents.impersonation.relay.record_degradation", degradation)
    abort = Mock()
    monkeypatch.setattr("base.agents.impersonation.abort_lease", abort)
    await impersonation.claim_gate(BaseAgentState(), 42, native_gate_ctx)
    degradation.assert_called_once()
    assert "unsupported" in degradation.call_args.args[-1]
    abort.assert_not_called()


@pytest.mark.parametrize("states", [["dead"], ["reused"], ["dead", "reused"]])
async def test_claim_gate_stops_the_lease_when_the_executor_anchors_are_gone(
    monkeypatch: pytest.MonkeyPatch,
    incarnation: RuntimeIncarnation,
    states: list[str],
    native_gate_ctx: AvaContext,
) -> None:
    """Component A: all recorded provider anchors dead/reused stops the lease
    even with a fresh relay heartbeat (task #3998)."""
    fresh = _relay_session("active", relay_heartbeat_at=datetime.now(UTC))
    monkeypatch.setattr(impersonation, "native_status", AsyncMock(return_value=fresh))
    monkeypatch.setattr(impersonation, "_provider_anchor_states", Mock(return_value=list(states)))
    abort = Mock(return_value={"id": "lease-1"})
    monkeypatch.setattr("base.agents.impersonation.abort_lease", abort)
    from agent.nodes import END

    decision = await impersonation.claim_gate(BaseAgentState(), 42, native_gate_ctx)
    assert decision is not None and decision.goto == END
    abort.assert_called_once()
    assert abort.call_args.args[4] == "the executor process is gone"


async def test_repeated_unknown_executor_evidence_keeps_original_authority(
    monkeypatch: pytest.MonkeyPatch,
    incarnation: RuntimeIncarnation,
    native_gate_ctx: AvaContext,
) -> None:
    session = _relay_session(
        "active", expires_at=datetime.now(UTC), relay_heartbeat_at=datetime.now(UTC)
    )
    before = dict(session)
    monkeypatch.setattr(impersonation, "native_status", AsyncMock(return_value=session))
    monkeypatch.setattr(impersonation, "_provider_anchor_states", Mock(return_value=["denied"]))
    degradation = Mock()
    monkeypatch.setattr("base.agents.impersonation.relay.record_degradation", degradation)
    abort = Mock()
    monkeypatch.setattr("base.agents.impersonation.abort_lease", abort)
    for _ in range(3):
        await impersonation.claim_gate(BaseAgentState(), 42, native_gate_ctx)
    assert degradation.call_count == 3
    assert "unknown" in degradation.call_args.args[-1]
    assert session == before
    abort.assert_not_called()


async def test_claim_gate_records_unknown_without_recorded_anchors(
    monkeypatch: pytest.MonkeyPatch, incarnation: RuntimeIncarnation, native_gate_ctx: AvaContext
) -> None:
    """Absent legacy evidence is unknown, never folded into all-dead authority."""
    session = _relay_session("active", relay_heartbeat_at=datetime.now(UTC))
    monkeypatch.setattr(impersonation, "native_status", AsyncMock(return_value=session))
    monkeypatch.setattr(impersonation, "_provider_anchor_states", Mock(return_value=[]))
    degradation = Mock()
    monkeypatch.setattr("base.agents.impersonation.relay.record_degradation", degradation)
    abort = Mock(return_value={"id": "lease-1"})
    monkeypatch.setattr("base.agents.impersonation.abort_lease", abort)
    from agent.nodes import END

    decision = await impersonation.claim_gate(BaseAgentState(), 42, native_gate_ctx)
    assert decision is not None and decision.goto == END
    abort.assert_not_called()
    assert "unknown" in degradation.call_args.args[-1]


@pytest.mark.parametrize("fresh", [False, True])
async def test_unknown_previous_relay_withholds_spawn_without_ending_lease(
    monkeypatch: pytest.MonkeyPatch,
    incarnation: RuntimeIncarnation,
    native_gate_ctx: AvaContext,
    fresh: bool,
) -> None:
    from base.native_process.ownership import OwnedProcess

    session = _relay_session(
        "active",
        relay_heartbeat_at=datetime.now(UTC) if fresh else None,
        relay_identity={"pid": 123, "birth": 1.0, "starttime": 1},
    )
    before = dict(session)
    monkeypatch.setattr(impersonation, "native_status", AsyncMock(return_value=session))
    monkeypatch.setattr(impersonation, "_provider_anchor_states", Mock(return_value=["alive"]))
    monkeypatch.setattr(OwnedProcess, "live", Mock(side_effect=RuntimeError("birth unavailable")))
    degradation = Mock()
    monkeypatch.setattr("base.agents.impersonation.relay.record_degradation", degradation)
    provision = Mock()
    monkeypatch.setattr("base.agents.impersonation.provision_relay", provision)
    decision = await impersonation.claim_gate(BaseAgentState(), 42, native_gate_ctx)
    assert decision is not None and decision.goto == END
    assert degradation.call_count == int(not fresh)
    provision.assert_not_called()
    assert session == before
