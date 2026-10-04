"""Takeover barriers: consent, resource closure, checkpoint ordering and replay."""

from collections.abc import Iterator
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
from base.native_process.turn_identity import bind_turn_identity
from tests.impersonation_support import attested_caller, recorded_tree


@pytest.fixture
def relays() -> RelaySupervision:
    return RelaySupervision()


@pytest.fixture
def gate_ctx(database: Database, event_bus: EventBus, relays: RelaySupervision) -> AvaContext:
    return AvaContext(db=database, bus=event_bus, relays=relays)


@pytest.fixture
def incarnation() -> Iterator[RuntimeIncarnation]:
    token = RuntimeIncarnation(42, uuid4(), uuid4())
    with bind_turn_identity(token.agent_id, incarnation=token):
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
    monkeypatch.setattr(impersonation, "hosted_resources_settled", lambda: False)
    with pytest.raises(RuntimeError, match="unresolved native exec"):
        await impersonation.settle_checkpoint(MagicMock(), database, event_bus, 42, relays)
    activate.assert_not_called()
    assert not await impersonation.settle_checkpoint(
        MagicMock(), database, event_bus, 42, relays, activate_accepted=False
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
        await impersonation.settle_checkpoint(graph, database, event_bus, 42, relays)
    assert (await graph.aget_state(config)).values["counter"] == 3
    receipt.side_effect = None
    await impersonation.settle_checkpoint(graph, database, event_bus, 42, relays)
    assert (await graph.aget_state(config)).values["counter"] == 3
    assert receipt.call_count == 2


def test_accept_stops_exec_and_uses_captured_incarnation(
    monkeypatch: pytest.MonkeyPatch,
    incarnation: RuntimeIncarnation,
) -> None:
    from ava.impersonation import accept

    accepted = Mock()
    monkeypatch.setattr("base.agents.impersonation.accept", accepted)
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
    write_request(path, code="pass", agent_id=42, timeout_s=10, state={})
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
    batch = await claim_inbound_batch(aops_pool, agent_id, lifecycle_only=True)
    assert batch == []
    assert db_conn.execute(
        "SELECT kind FROM inbound_messages WHERE agent_id=%s AND status='pending' ORDER BY kind",
        (agent_id,),
    ).fetchall() == [("cancel",), ("chat",), ("compact_request",)]
    db_conn.commit()
    assert not await impersonation.lifecycle_ready(aops_pool, agent_id)
    # If the external holder never acknowledges cancellation, the ordinary
    # native claim after release/expiry still receives the durable request.
    resumed = await claim_inbound_batch(aops_pool, agent_id)
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
        aops_pool, agent_id, machine_name(), uuid4(), expected_from="idling", db=database
    )
    assert owner is not None
    db_conn.execute(
        "INSERT INTO inbound_messages(agent_id,content,kind,source) VALUES(%s,'',%s,'user')",
        (agent_id, kind),
    )
    db_conn.commit()
    with bind_turn_identity(agent_id, incarnation=owner):
        accepted = await claim_inbound_batch(aops_pool, agent_id, lifecycle_only=True)
    assert len(accepted) == 1 and accepted[0].durable_lifecycle
    replacement = RuntimeIncarnation(agent_id, uuid4(), uuid4())
    db_conn.execute(
        "UPDATE agents_meta SET runtime_generation=%s,runtime_owner=%s WHERE id=%s",
        (replacement.generation, replacement.owner, agent_id),
    )
    db_conn.commit()
    with bind_turn_identity(agent_id, incarnation=replacement):
        assert await claim_inbound_batch(aops_pool, agent_id, lifecycle_only=True) == []
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
        await claim_inbound_batch(aops_pool, agent_id, lifecycle_only=True)
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
    monkeypatch.setattr(impersonation, "hosted_resources_settled", lambda: True)

    def refused(*_args: object) -> bool:
        return False

    monkeypatch.setattr(impersonation, "establish_relay", refused)
    assert not await impersonation.settle_checkpoint(MagicMock(), database, event_bus, 42, relays)
    activate.assert_not_called()


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
    monkeypatch.setattr(impersonation, "hosted_resources_settled", lambda: True)
    establish = Mock(return_value=True)
    monkeypatch.setattr(impersonation, "establish_relay", establish)
    assert await impersonation.settle_checkpoint(MagicMock(), database, event_bus, 42, relays)
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

    provision = Mock()
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
    spawn.assert_called_once_with(42, "lease-1", provision_token, "thread-1", None, None)
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
    monkeypatch.setattr("base.agents.impersonation.provision_relay", Mock())
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
    monkeypatch.setattr("base.agents.impersonation.provision_relay", Mock())
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


async def test_claim_gate_tears_down_the_relay_when_control_returns(
    monkeypatch: pytest.MonkeyPatch,
    gate_ctx: AvaContext,
) -> None:
    monkeypatch.setattr(impersonation, "native_status", AsyncMock(return_value=None))
    process = MagicMock()
    process.poll.return_value = None
    gate_ctx.relays.children[42] = RelayChild("lease-1", process, "token", 0.0)
    terminate = Mock()
    monkeypatch.setattr(impersonation, "_terminate_relay", terminate)
    assert await impersonation.claim_gate(BaseAgentState(), 42, gate_ctx) is None
    terminate.assert_called_once()
    assert 42 not in gate_ctx.relays.children


async def test_claim_gate_stops_the_lease_when_its_minted_codex_relay_died(
    monkeypatch: pytest.MonkeyPatch,
    incarnation: RuntimeIncarnation,
    gate_ctx: AvaContext,
) -> None:
    """A relay this process minted (its token is held) that stopped
    heartbeating stops the lease — no respawn (task #3998)."""
    from datetime import UTC, datetime, timedelta

    stale = _relay_session("active", relay_heartbeat_at=datetime.now(UTC) - timedelta(minutes=5))
    monkeypatch.setattr(impersonation, "native_status", AsyncMock(return_value=stale))
    dead = MagicMock()
    dead.poll.return_value = 1
    gate_ctx.relays.children[42] = RelayChild("lease-1", dead, "old-token", 0.0)
    abort = Mock(return_value={"id": "lease-1"})
    monkeypatch.setattr("base.agents.impersonation.abort_lease", abort)
    spawn = Mock()
    monkeypatch.setattr(impersonation, "_spawn_codex_relay", spawn)
    from agent.nodes import END

    decision = await impersonation.claim_gate(BaseAgentState(), 42, gate_ctx)
    assert decision is not None and decision.goto == END
    abort.assert_called_once()
    assert abort.call_args.args[2] == "lease-1"
    assert abort.call_args.args[4] == "the bound relay stopped heartbeating"
    spawn.assert_not_called()
    assert 42 not in gate_ctx.relays.children


async def test_claim_gate_respects_startup_grace_for_a_fresh_spawn(
    monkeypatch: pytest.MonkeyPatch,
    incarnation: RuntimeIncarnation,
    gate_ctx: AvaContext,
) -> None:
    from datetime import UTC, datetime, timedelta

    stale = _relay_session("active", relay_heartbeat_at=datetime.now(UTC) - timedelta(minutes=5))
    monkeypatch.setattr(impersonation, "native_status", AsyncMock(return_value=stale))
    alive = MagicMock()
    alive.poll.return_value = None
    now = impersonation.time.monotonic()
    gate_ctx.relays.children[42] = RelayChild("lease-1", alive, "token", now)
    spawn = Mock()
    monkeypatch.setattr(impersonation, "_spawn_codex_relay", spawn)
    await impersonation.claim_gate(BaseAgentState(), 42, gate_ctx)
    spawn.assert_not_called()


async def test_claim_gate_stops_a_stale_claude_relay(
    monkeypatch: pytest.MonkeyPatch, incarnation: RuntimeIncarnation, gate_ctx: AvaContext
) -> None:
    """A claude controller-session relay whose heartbeat went stale stops the
    lease — it can never be re-provisioned from this side (task #3998)."""
    from datetime import UTC, datetime, timedelta

    stale = _relay_session(
        "active",
        provider="claude",
        relay_heartbeat_at=datetime.now(UTC) - timedelta(minutes=5),
    )
    monkeypatch.setattr(impersonation, "native_status", AsyncMock(return_value=stale))
    from agent.nodes import END

    record = Mock(return_value=True)
    monkeypatch.setattr("base.agents.impersonation.record_relay_failure", record)
    abort = Mock(return_value={"id": "lease-1"})
    monkeypatch.setattr("base.agents.impersonation.abort_lease", abort)
    decision = await impersonation.claim_gate(BaseAgentState(), 42, gate_ctx)
    assert decision is not None and decision.goto == END
    abort.assert_called_once()
    assert abort.call_args.args[4] == "the bound relay stopped heartbeating"
    record.assert_not_called()


@pytest.mark.parametrize("states", [["dead"], ["reused"], ["dead", "reused"]])
async def test_claim_gate_stops_the_lease_when_the_executor_anchors_are_gone(
    monkeypatch: pytest.MonkeyPatch,
    incarnation: RuntimeIncarnation,
    states: list[str],
    gate_ctx: AvaContext,
) -> None:
    """Component A: all recorded provider anchors dead/reused stops the lease
    even with a fresh relay heartbeat (task #3998)."""
    fresh = _relay_session("active", relay_heartbeat_at=datetime.now(UTC))
    monkeypatch.setattr(impersonation, "native_status", AsyncMock(return_value=fresh))
    monkeypatch.setattr(impersonation, "_provider_anchor_states", Mock(return_value=list(states)))
    abort = Mock(return_value={"id": "lease-1"})
    monkeypatch.setattr("base.agents.impersonation.abort_lease", abort)
    from agent.nodes import END

    decision = await impersonation.claim_gate(BaseAgentState(), 42, gate_ctx)
    assert decision is not None and decision.goto == END
    abort.assert_called_once()
    assert abort.call_args.args[4] == "the executor process is gone"


async def test_claim_gate_waits_a_second_anchor_pass_before_the_verdict(
    monkeypatch: pytest.MonkeyPatch,
    incarnation: RuntimeIncarnation,
    gate_ctx: AvaContext,
) -> None:
    """Unreadable (AccessDenied/unknown) anchors are never a single-pass death
    verdict; the second consecutive pass stops the lease (task #3998)."""
    session = _relay_session("active", relay_heartbeat_at=datetime.now(UTC))
    monkeypatch.setattr(impersonation, "native_status", AsyncMock(return_value=session))
    monkeypatch.setattr(impersonation, "_provider_anchor_states", Mock(return_value=["denied"]))
    abort = Mock(return_value={"id": "lease-1"})
    monkeypatch.setattr("base.agents.impersonation.abort_lease", abort)

    await impersonation.claim_gate(BaseAgentState(), 42, gate_ctx)
    abort.assert_not_called()
    assert gate_ctx.relays.anchor_obscured.get(42) == "lease-1"
    await impersonation.claim_gate(BaseAgentState(), 42, gate_ctx)
    abort.assert_called_once()


async def test_claim_gate_skips_the_anchor_verdict_without_recorded_anchors(
    monkeypatch: pytest.MonkeyPatch, incarnation: RuntimeIncarnation, gate_ctx: AvaContext
) -> None:
    """No anchors at all (legacy records) is skipped, never folded into
    "all dead": a fresh heartbeat keeps the lease (task #3998)."""
    session = _relay_session("active", relay_heartbeat_at=datetime.now(UTC))
    monkeypatch.setattr(impersonation, "native_status", AsyncMock(return_value=session))
    monkeypatch.setattr(impersonation, "_provider_anchor_states", Mock(return_value=[]))
    abort = Mock(return_value={"id": "lease-1"})
    monkeypatch.setattr("base.agents.impersonation.abort_lease", abort)
    from agent.nodes import END

    decision = await impersonation.claim_gate(BaseAgentState(), 42, gate_ctx)
    assert decision is not None and decision.goto == END
    abort.assert_not_called()


async def test_claim_gate_terminates_a_hung_relay_before_stopping_the_lease(
    monkeypatch: pytest.MonkeyPatch,
    incarnation: RuntimeIncarnation,
    gate_ctx: AvaContext,
) -> None:
    """A live relay beyond the startup grace that stopped heartbeating is
    terminated as the lease stops — no lingering process (task #3998)."""
    from datetime import UTC, datetime, timedelta

    stale = _relay_session("active", relay_heartbeat_at=datetime.now(UTC) - timedelta(minutes=5))
    monkeypatch.setattr(impersonation, "native_status", AsyncMock(return_value=stale))
    alive = MagicMock()
    alive.poll.return_value = None
    gate_ctx.relays.children[42] = RelayChild("lease-1", alive, "token", 0.0)
    terminate = Mock()
    monkeypatch.setattr(impersonation, "_terminate_relay", terminate)
    abort = Mock(return_value={"id": "lease-1"})
    monkeypatch.setattr("base.agents.impersonation.abort_lease", abort)

    await impersonation.claim_gate(BaseAgentState(), 42, gate_ctx)
    terminate.assert_called_once()
    abort.assert_called_once()
    assert 42 not in gate_ctx.relays.children


async def test_claim_gate_reprovisions_a_restart_lost_codex_relay_inside_the_window(
    monkeypatch: pytest.MonkeyPatch,
    incarnation: RuntimeIncarnation,
    gate_ctx: AvaContext,
) -> None:
    """The narrow carve-out: this process never minted the relay, the last beat
    predates our boot and the fresh-start window is open — re-provision
    (task #3998, variant A)."""
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    gate_ctx.relays.started_wall = now
    gate_ctx.relays.started_monotonic = impersonation.time.monotonic()
    monkeypatch.setattr(
        "base.config.settings.agent.impersonation_reprovision_window_seconds", 120.0
    )
    stale = _relay_session("active", relay_heartbeat_at=now - timedelta(minutes=5))
    monkeypatch.setattr(impersonation, "native_status", AsyncMock(return_value=stale))
    provision = Mock()
    monkeypatch.setattr("base.agents.impersonation.provision_relay", provision)
    new_process = MagicMock()
    new_process.poll.return_value = None
    spawn = Mock(return_value=new_process)
    monkeypatch.setattr(impersonation, "_spawn_codex_relay", spawn)
    abort = Mock(return_value={"id": "lease-1"})
    monkeypatch.setattr("base.agents.impersonation.abort_lease", abort)
    from agent.nodes import END

    decision = await impersonation.claim_gate(BaseAgentState(), 42, gate_ctx)
    assert decision is not None and decision.goto == END
    provision.assert_called_once()
    spawn.assert_called_once()
    abort.assert_not_called()
    assert gate_ctx.relays.children[42].process is new_process


async def test_claim_gate_stops_a_restart_lost_relay_that_beat_after_boot(
    monkeypatch: pytest.MonkeyPatch,
    incarnation: RuntimeIncarnation,
    gate_ctx: AvaContext,
) -> None:
    """A relay that demonstrably beat after this process started died under our
    watch — the restart-shaped carve-out does not apply (task #3998)."""
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    gate_ctx.relays.started_wall = now - timedelta(seconds=100)
    gate_ctx.relays.started_monotonic = impersonation.time.monotonic() - 100.0
    monkeypatch.setattr(
        "base.config.settings.agent.impersonation_reprovision_window_seconds", 120.0
    )
    stale = _relay_session("active", relay_heartbeat_at=now - timedelta(seconds=60))
    monkeypatch.setattr(impersonation, "native_status", AsyncMock(return_value=stale))
    provision = Mock()
    monkeypatch.setattr("base.agents.impersonation.provision_relay", provision)
    abort = Mock(return_value={"id": "lease-1"})
    monkeypatch.setattr("base.agents.impersonation.abort_lease", abort)

    await impersonation.claim_gate(BaseAgentState(), 42, gate_ctx)
    abort.assert_called_once()
    assert abort.call_args.args[4] == "the bound relay stopped heartbeating"
    provision.assert_not_called()


async def test_claim_gate_stops_a_relay_minted_by_the_current_incarnation(
    monkeypatch: pytest.MonkeyPatch,
    incarnation: RuntimeIncarnation,
    gate_ctx: AvaContext,
) -> None:
    """A durable mint mark naming this incarnation rules out the restart-shaped
    loss (the in-memory record only went missing) — stop (task #3998)."""
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    gate_ctx.relays.started_wall = now
    gate_ctx.relays.started_monotonic = impersonation.time.monotonic()
    stale = _relay_session(
        "active",
        relay_heartbeat_at=now - timedelta(minutes=5),
        relay_minted_generation=str(incarnation.generation),
    )
    monkeypatch.setattr(impersonation, "native_status", AsyncMock(return_value=stale))
    spawn = Mock()
    monkeypatch.setattr(impersonation, "_spawn_codex_relay", spawn)
    abort = Mock(return_value={"id": "lease-1"})
    monkeypatch.setattr("base.agents.impersonation.abort_lease", abort)

    await impersonation.claim_gate(BaseAgentState(), 42, gate_ctx)
    abort.assert_called_once()
    spawn.assert_not_called()


async def test_claim_gate_stops_the_restart_lost_relay_outside_the_window(
    monkeypatch: pytest.MonkeyPatch,
    incarnation: RuntimeIncarnation,
    gate_ctx: AvaContext,
) -> None:
    """Outside the fresh-start window the restart-shaped loss stops the lease:
    the carve-out must not degenerate into respawn-forever (task #3998)."""
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    gate_ctx.relays.started_wall = now
    gate_ctx.relays.started_monotonic = impersonation.time.monotonic() - 1000.0
    stale = _relay_session("active", relay_heartbeat_at=now - timedelta(minutes=5))
    monkeypatch.setattr(impersonation, "native_status", AsyncMock(return_value=stale))
    provision = Mock()
    monkeypatch.setattr("base.agents.impersonation.provision_relay", provision)
    abort = Mock(return_value={"id": "lease-1"})
    monkeypatch.setattr("base.agents.impersonation.abort_lease", abort)

    await impersonation.claim_gate(BaseAgentState(), 42, gate_ctx)
    abort.assert_called_once()
    provision.assert_not_called()


def test_reprovision_window_follows_the_configured_constant(
    monkeypatch: pytest.MonkeyPatch, relays: RelaySupervision
) -> None:
    """The carve-out window is the config knob; 0 disables the exception."""
    relays.started_monotonic = impersonation.time.monotonic()
    monkeypatch.setattr(
        "base.config.settings.agent.impersonation_reprovision_window_seconds", 120.0
    )
    assert impersonation._reprovision_window_active(relays)
    relays.started_monotonic = 0.0
    assert not impersonation._reprovision_window_active(relays)
    relays.started_monotonic = impersonation.time.monotonic()
    monkeypatch.setattr("base.config.settings.agent.impersonation_reprovision_window_seconds", 0.0)
    assert not impersonation._reprovision_window_active(relays)


def test_relay_beat_predates_boot_reads_the_process_start(
    monkeypatch: pytest.MonkeyPatch, relays: RelaySupervision
) -> None:
    """No heartbeat, or a beat before this process's boot, is restart-shaped."""
    from datetime import timedelta

    boot = datetime.now(UTC)
    relays.started_wall = boot
    assert impersonation._relay_beat_predates_boot(None, relays)
    assert impersonation._relay_beat_predates_boot(boot - timedelta(seconds=1), relays)
    assert not impersonation._relay_beat_predates_boot(boot + timedelta(seconds=1), relays)


async def test_successor_admission_aligns_active_lease_binding_before_release(
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    database: Database,
    event_bus: EventBus,
) -> None:
    """Issue #2052: a lease released between hosted restart and the first
    native_status must not write the dead incarnation back into agents_meta.

    The successor admission aligns the active lease's accepted_* binding in
    the admission transaction, so the restore trigger's write-back is already
    a no-op when the controller releases before any held wake.
    """
    from agent.ownership.hosted import admit_hosted_runtime
    from base.agents import impersonation as leases
    from base.agents.messages.caller_identity import CallerIdentity
    from base.cluster.machine import machine_name
    from tests.fixtures.units import spawn_agent

    agent_id = spawn_agent()
    first = await admit_hosted_runtime(
        aops_pool, agent_id, machine_name(), uuid4(), expected_from="idling", db=database
    )
    assert first is not None
    lease = leases.request(
        database,
        event_bus,
        agent_id,
        caller=CallerIdentity(kind="external_agent", subject="codex", instance="test"),
        ttl_seconds=3600,
        reason="Handle the next message",
        process_metadata=recorded_tree(),
        relay_provider="codex",
        relay_thread_id=str(uuid4()),
    )
    leases.accept(database, event_bus, lease["id"], agent_id, first, "Handoff brief")
    leases.activate(database, event_bus, lease["id"], first)
    db_conn.execute(
        "UPDATE agents_meta SET lease_expires_at = clock_timestamp() - interval '1 second' "
        "WHERE id=%s",
        (agent_id,),
    )
    db_conn.commit()
    successor = await admit_hosted_runtime(
        aops_pool, agent_id, machine_name(), uuid4(), expected_from="running", db=database
    )
    assert successor is not None
    assert successor.generation != first.generation
    assert db_conn.execute(
        "SELECT accepted_generation,accepted_owner FROM agent_impersonations WHERE id=%s",
        (lease["id"],),
    ).fetchone() == (successor.generation, successor.owner)
    # Release before any native_status: the restore trigger fires, but the
    # binding already matches the live incarnation — agents_meta is untouched.
    leases.release(
        database, event_bus, lease["id"], attested_caller(lease), "Done before the first held wake"
    )
    db_conn.commit()
    assert db_conn.execute(
        "SELECT runtime_generation,runtime_owner FROM agents_meta WHERE id=%s",
        (agent_id,),
    ).fetchone() == (successor.generation, successor.owner)
