"""Takeover barriers: consent, resource closure, checkpoint ordering and replay."""

from collections.abc import Callable, Iterator
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any
from unittest.mock import ANY, AsyncMock, MagicMock, Mock
from uuid import uuid4

import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command
from pydantic import BaseModel, Field

import base.agents.impersonation as agents_impersonation
from agent import impersonation, impersonation_handoff, impersonation_process
from agent.extensions import plugin_state_codec
from agent.graph.exec._result import lifecycle_exception_from_name
from agent.graph.exec.protocol import read_request, write_request
from agent.hooks.tests.fakes import placeholder_runtime
from agent.state import BaseAgentState
from base.agents.context import AvaContext
from base.agents.impersonation import relay
from base.agents.impersonation.notes import HandoffNotes
from base.agents.lifecycle import AgentImpersonation
from base.agents.observation.relay_supervision import RelayChild, RelaySupervision
from base.clock import Clock, clock_config_from_boot
from base.cluster.machine import MachineIdentity
from base.config import settings
from base.config.tests.configuration import snapshot_process_config
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.host.env.agent_slices import AgentSlices
from base.native_process.runtime_incarnation import RuntimeIncarnation
from base.packages.plugin_providers import build_model_catalog
from tests.fixtures.pin_agent import exec_context, pin_agent


def _notes() -> HandoffNotes:
    return HandoffNotes(Clock.from_settings, lambda: settings.general.message_timestamps)


@pytest.fixture
def relays() -> RelaySupervision:
    return RelaySupervision()


@pytest.fixture
def gate_ctx(
    database: Database,
    event_bus: EventBus,
    relays: RelaySupervision,
    machine_identity: MachineIdentity,
) -> AvaContext:
    config = snapshot_process_config()
    return AvaContext(
        db=database,
        bus=event_bus,
        machine=machine_identity,
        relays=relays,
        catalog=build_model_catalog(),
        clock_factory=lambda: Clock(clock_config_from_boot(config)),
        agent=AgentSlices.resolve(
            default_reader=lambda domain, field: getattr(getattr(config.view, domain), field)
        ),
    )


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


@pytest.mark.parametrize("status", ["accepted", "active", "released", "expired"])
async def test_hold_ends_before_claim_or_compaction(
    monkeypatch: pytest.MonkeyPatch,
    status: str,
    gate_ctx: AvaContext,
    *,
    database_gate: ProcessDbGate,
) -> None:
    monkeypatch.setattr(impersonation, "native_status", AsyncMock(return_value=_session(status)))
    decision = await impersonation.claim_gate(BaseAgentState(), 42, gate_ctx)
    assert decision is not None
    assert decision.goto == END
    hook = AsyncMock(return_value=Command(goto="llm"))
    guarded = impersonation.protect_native_hooks(hook)
    result = await guarded(
        BaseAgentState(),
        placeholder_runtime(database_gate=database_gate),
        {"configurable": {"thread_id": "42"}},
    )
    assert result.goto == END
    hook.assert_not_awaited()


async def test_checkpoint_receipt_prevents_reapplying_non_idempotent_delta(
    monkeypatch: pytest.MonkeyPatch,
    incarnation: RuntimeIncarnation,
    database: Database,
    event_bus: EventBus,
    relays: RelaySupervision,
    machine_identity: MachineIdentity,
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

    monkeypatch.setattr(plugin_state_codec, "decode_plugin_delta", decode)
    receipt = Mock(side_effect=RuntimeError("receipt commit lost"))
    monkeypatch.setattr(agents_impersonation, "mark_plugin_applied", receipt)
    with pytest.raises(RuntimeError, match="receipt commit lost"):
        await impersonation.settle_checkpoint(
            graph,
            database,
            event_bus,
            42,
            relays,
            incarnation=incarnation,
            resources=None,
            notes=_notes(),
            read_local_machine=machine_identity.name,
        )
    assert (await graph.aget_state(config)).values["counter"] == 3
    receipt.side_effect = None
    await impersonation.settle_checkpoint(
        graph,
        database,
        event_bus,
        42,
        relays,
        incarnation=incarnation,
        resources=None,
        notes=_notes(),
        read_local_machine=machine_identity.name,
    )
    assert (await graph.aget_state(config)).values["counter"] == 3
    assert receipt.call_count == 2


def test_accept_stops_exec_and_uses_captured_incarnation(
    monkeypatch: pytest.MonkeyPatch,
    incarnation: RuntimeIncarnation,
    machine_identity: MachineIdentity,
) -> None:
    from ava.impersonation import accept

    accepted = Mock()
    monkeypatch.setattr(agents_impersonation, "accept", accepted)
    pin_agent(incarnation.agent_id, incarnation=incarnation)
    with pytest.raises(AgentImpersonation):
        accept("lease-1", "Hand the task to the external session.")
    accepted.assert_called_once_with(
        ANY,
        ANY,
        "lease-1",
        42,
        incarnation,
        "Hand the task to the external session.",
        local_machine=machine_identity.name(),
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
    machine_identity: MachineIdentity,
) -> None:
    monkeypatch.setattr(
        impersonation, "native_status", AsyncMock(return_value=_relay_session("accepted"))
    )
    activate = Mock()
    monkeypatch.setattr(agents_impersonation, "activate", activate)
    checkpoint = AsyncMock()
    monkeypatch.setattr(impersonation_handoff, "ensure_start_marker", checkpoint)

    def refused(*_args: object, read_local_machine: Callable[[], str]) -> bool:
        return False

    monkeypatch.setattr(impersonation, "establish_relay", refused)
    assert not await impersonation.settle_checkpoint(
        MagicMock(),
        database,
        event_bus,
        42,
        relays,
        incarnation=incarnation,
        resources=None,
        notes=_notes(),
        read_local_machine=machine_identity.name,
    )
    activate.assert_not_called()
    checkpoint.assert_awaited_once()


async def test_settle_checkpoint_activates_only_after_relay_ready(
    monkeypatch: pytest.MonkeyPatch,
    incarnation: RuntimeIncarnation,
    database: Database,
    event_bus: EventBus,
    relays: RelaySupervision,
    machine_identity: MachineIdentity,
) -> None:
    monkeypatch.setattr(
        impersonation, "native_status", AsyncMock(return_value=_relay_session("accepted"))
    )
    activate = Mock(return_value=_relay_session("active"))
    monkeypatch.setattr(agents_impersonation, "activate", activate)
    checkpoint = AsyncMock()
    monkeypatch.setattr(impersonation_handoff, "ensure_start_marker", checkpoint)
    establish = Mock(return_value=True)
    monkeypatch.setattr(impersonation, "establish_relay", establish)
    assert await impersonation.settle_checkpoint(
        MagicMock(),
        database,
        event_bus,
        42,
        relays,
        incarnation=incarnation,
        resources=None,
        notes=_notes(),
        read_local_machine=machine_identity.name,
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
    machine_identity: MachineIdentity,
) -> None:
    from datetime import UTC, datetime, timedelta

    fail = Mock()
    monkeypatch.setattr(agents_impersonation, "fail_acceptance", fail)
    stale = _relay_session(
        "accepted", provider=provider, relay_heartbeat_at=datetime.now(UTC) - timedelta(minutes=5)
    )
    assert (
        impersonation.establish_relay(
            database,
            event_bus,
            stale,
            incarnation,
            relays,
            read_local_machine=machine_identity.name,
        )
        is False
    )
    fail.assert_called_once()
    fail.reset_mock()
    fresh = _relay_session("accepted", provider=provider, relay_heartbeat_at=datetime.now(UTC))
    assert (
        impersonation.establish_relay(
            database,
            event_bus,
            fresh,
            incarnation,
            relays,
            read_local_machine=machine_identity.name,
        )
        is True
    )
    fail.assert_not_called()


def test_establish_relay_codex_provisions_spawns_and_waits_for_heartbeat(
    monkeypatch: pytest.MonkeyPatch,
    incarnation: RuntimeIncarnation,
    database: Database,
    event_bus: EventBus,
    relays: RelaySupervision,
    machine_identity: MachineIdentity,
) -> None:
    from datetime import UTC, datetime

    provision = Mock(return_value={"relay_generation": 1})
    monkeypatch.setattr(agents_impersonation, "provision_relay", provision)
    spawn = Mock(return_value=MagicMock(poll=Mock(return_value=None)))
    monkeypatch.setattr(impersonation_process, "spawn_codex_relay", spawn)
    ready = {"status": "accepted", "relay_heartbeat_at": datetime.now(UTC)}
    monkeypatch.setattr(agents_impersonation, "relay_get", Mock(return_value=ready))
    assert (
        impersonation.establish_relay(
            database,
            event_bus,
            _relay_session(),
            incarnation,
            relays,
            read_local_machine=machine_identity.name,
        )
        is True
    )
    provision.assert_called_once()
    provision_token = provision.call_args.args[3]
    assert spawn.call_count == 1
    assert spawn.call_args.args == (42, "lease-1", provision_token, "thread-1", None, None)
    assert relays.children[42].token == provision_token


def test_establish_relay_codex_spawn_exit_rolls_back(
    monkeypatch: pytest.MonkeyPatch,
    incarnation: RuntimeIncarnation,
    database: Database,
    event_bus: EventBus,
    relays: RelaySupervision,
    machine_identity: MachineIdentity,
) -> None:
    child = MagicMock()
    child.poll.return_value = 5
    child.returncode = 5
    monkeypatch.setattr(
        agents_impersonation, "provision_relay", Mock(return_value={"relay_generation": 1})
    )
    monkeypatch.setattr(impersonation_process, "spawn_codex_relay", Mock(return_value=child))
    fail = Mock()
    monkeypatch.setattr(agents_impersonation, "fail_acceptance", fail)
    assert (
        impersonation.establish_relay(
            database,
            event_bus,
            _relay_session(),
            incarnation,
            relays,
            read_local_machine=machine_identity.name,
        )
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
    machine_identity: MachineIdentity,
) -> None:
    child = MagicMock()
    child.poll.return_value = None
    monkeypatch.setattr(
        agents_impersonation, "provision_relay", Mock(return_value={"relay_generation": 1})
    )
    monkeypatch.setattr(impersonation_process, "spawn_codex_relay", Mock(return_value=child))
    terminate = Mock()
    monkeypatch.setattr(impersonation, "terminate_relay", terminate)
    fail = Mock()
    monkeypatch.setattr(agents_impersonation, "fail_acceptance", fail)
    monkeypatch.setattr(impersonation, "RELAY_READY_TIMEOUT_S", 0.0)
    assert (
        impersonation.establish_relay(
            database,
            event_bus,
            _relay_session(),
            incarnation,
            relays,
            read_local_machine=machine_identity.name,
        )
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
    monkeypatch.setattr(impersonation, "terminate_relay", terminate)
    await impersonation.supervise_relay(
        db,
        MagicMock(),
        None,
        42,
        gate_ctx.relays,
        incarnation=None,
    )
    terminate.assert_not_called()
    assert gate_ctx.relays.children[42].process is process


@pytest.mark.parametrize("same_lease", [False, True])
async def test_delayed_snapshot_cannot_retire_replacement_sender(
    monkeypatch: pytest.MonkeyPatch,
    gate_ctx: AvaContext,
    same_lease: bool,
    machine_identity: MachineIdentity,
) -> None:
    session = _relay_session("active", relay_generation=1)
    process = MagicMock()
    gate_ctx.relays.children[42] = RelayChild(
        "lease-1" if same_lease else "lease-2", process, "new-token", 0.0, 2
    )
    terminate = Mock()
    monkeypatch.setattr(impersonation, "terminate_relay", terminate)
    await impersonation.supervise_relay(
        MagicMock(),
        MagicMock(),
        session,
        42,
        gate_ctx.relays,
        incarnation=None,
    )
    terminate.assert_not_called()
    assert gate_ctx.relays.children[42].process is process


@pytest.mark.parametrize("owned", [False, True])
@pytest.mark.parametrize("running", [False, True])
async def test_claim_gate_records_a_stale_relay_as_degraded_and_never_replaces_it(
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
    monkeypatch.setattr(impersonation, "provider_anchor_states", Mock(return_value=["alive"]))
    old = MagicMock()
    old.poll.return_value = None if running else 1
    if owned:
        native_gate_ctx.relays.children[42] = RelayChild("lease-1", old, "token", 0.0, 1)
    terminated = Mock()
    monkeypatch.setattr(impersonation, "terminate_relay", terminated)
    provision = Mock(return_value={"relay_generation": 2})
    monkeypatch.setattr(agents_impersonation, "provision_relay", provision)
    spawn = Mock()
    monkeypatch.setattr(impersonation_process, "spawn_codex_relay", spawn)
    degradation = Mock()
    monkeypatch.setattr(relay, "record_degradation", degradation)
    stamp = Mock()
    monkeypatch.setattr(impersonation, "stamp_relay_failure", stamp)
    abort = Mock()
    monkeypatch.setattr(agents_impersonation, "abort_lease", abort)
    decision = await impersonation.claim_gate(BaseAgentState(), 42, native_gate_ctx)
    assert decision is not None and decision.goto == END
    # No replacement is ever minted or spawned, and the stale child is left alone.
    provision.assert_not_called()
    spawn.assert_not_called()
    terminated.assert_not_called()
    abort.assert_not_called()
    degradation.assert_called_once()
    stamp.assert_called_once()
    if owned:
        assert native_gate_ctx.relays.children[42].process is old


async def test_startup_grace_does_not_degrade_a_fresh_child(
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
    degradation = Mock()
    monkeypatch.setattr(relay, "record_degradation", degradation)
    await impersonation.claim_gate(BaseAgentState(), 42, native_gate_ctx)
    degradation.assert_not_called()
    assert native_gate_ctx.relays.children[42].process is child


async def test_confirmed_child_exit_is_degraded_at_once_without_replacement(
    monkeypatch: pytest.MonkeyPatch,
    incarnation: RuntimeIncarnation,
    native_gate_ctx: AvaContext,
) -> None:
    from agent.nodes import END

    session = _relay_session(
        "active", relay_heartbeat_at=datetime.now(UTC), relay_minted_at=datetime.now(UTC)
    )
    old = MagicMock()
    old.poll.return_value = 1
    native_gate_ctx.relays.children[42] = RelayChild(
        "lease-1", old, "old-token", impersonation.time.monotonic(), 1
    )
    monkeypatch.setattr(impersonation, "native_status", AsyncMock(return_value=session))
    monkeypatch.setattr(impersonation, "provider_anchor_states", Mock(return_value=["alive"]))
    provision = Mock(return_value={"relay_generation": 2})
    monkeypatch.setattr(agents_impersonation, "provision_relay", provision)
    spawn = Mock()
    monkeypatch.setattr(impersonation_process, "spawn_codex_relay", spawn)
    degradation = Mock()
    monkeypatch.setattr(relay, "record_degradation", degradation)
    monkeypatch.setattr(impersonation, "stamp_relay_failure", Mock())
    decision = await impersonation.claim_gate(BaseAgentState(), 42, native_gate_ctx)
    assert decision is not None and decision.goto == END
    degradation.assert_called_once()
    provision.assert_not_called()
    spawn.assert_not_called()
    assert session["relay_generation"] == 1


async def test_stale_session_relay_retains_authority_with_visible_unsupported_recovery(
    monkeypatch: pytest.MonkeyPatch,
    incarnation: RuntimeIncarnation,
    native_gate_ctx: AvaContext,
) -> None:
    session = _relay_session("active", provider="claude")
    monkeypatch.setattr(impersonation, "provider_anchor_states", Mock(return_value=["alive"]))
    monkeypatch.setattr(impersonation, "native_status", AsyncMock(return_value=session))
    degradation = Mock()
    monkeypatch.setattr(relay, "record_degradation", degradation)
    monkeypatch.setattr(impersonation, "stamp_relay_failure", Mock())
    abort = Mock()
    monkeypatch.setattr(agents_impersonation, "abort_lease", abort)
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
    monkeypatch.setattr(impersonation, "provider_anchor_states", Mock(return_value=list(states)))
    abort = Mock(return_value={"id": "lease-1"})
    monkeypatch.setattr(agents_impersonation, "abort_lease", abort)
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
    monkeypatch.setattr(impersonation, "provider_anchor_states", Mock(return_value=["denied"]))
    degradation = Mock()
    monkeypatch.setattr(relay, "record_degradation", degradation)
    abort = Mock()
    monkeypatch.setattr(agents_impersonation, "abort_lease", abort)
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
    monkeypatch.setattr(impersonation, "provider_anchor_states", Mock(return_value=[]))
    degradation = Mock()
    monkeypatch.setattr(relay, "record_degradation", degradation)
    abort = Mock(return_value={"id": "lease-1"})
    monkeypatch.setattr(agents_impersonation, "abort_lease", abort)
    from agent.nodes import END

    decision = await impersonation.claim_gate(BaseAgentState(), 42, native_gate_ctx)
    assert decision is not None and decision.goto == END
    abort.assert_not_called()
    assert "unknown" in degradation.call_args.args[-1]


@pytest.mark.parametrize("fresh", [False, True])
async def test_unknown_relay_exit_evidence_degrades_only_when_the_heartbeat_is_stale(
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
    monkeypatch.setattr(impersonation, "provider_anchor_states", Mock(return_value=["alive"]))
    monkeypatch.setattr(OwnedProcess, "live", Mock(side_effect=RuntimeError("birth unavailable")))
    degradation = Mock()
    monkeypatch.setattr(relay, "record_degradation", degradation)
    monkeypatch.setattr(impersonation, "stamp_relay_failure", Mock())
    provision = Mock()
    monkeypatch.setattr(agents_impersonation, "provision_relay", provision)
    decision = await impersonation.claim_gate(BaseAgentState(), 42, native_gate_ctx)
    assert decision is not None and decision.goto == END
    assert degradation.call_count == int(not fresh)
    provision.assert_not_called()
    assert session == before
