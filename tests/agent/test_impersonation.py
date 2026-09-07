"""Takeover barriers: consent, resource closure, checkpoint ordering and replay."""

from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Annotated, Any, cast
from unittest.mock import AsyncMock, MagicMock, Mock
from uuid import uuid4

import psycopg
import pytest
from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import Command
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel, Field

from agent import impersonation
from agent.graph._exec_protocol import read_request, write_request
from agent.graph._exec_result import lifecycle_exception_from_name
from agent.state import BaseAgentState
from shared.context import AvaContext
from shared.lifecycle import AgentImpersonation
from shared.runtime_incarnation import RuntimeIncarnation
from shared.turn_identity import bind_turn_identity


@pytest.fixture
def incarnation() -> Iterator[RuntimeIncarnation]:
    token = RuntimeIncarnation(42, uuid4(), uuid4())
    with bind_turn_identity(token.agent_id, incarnation=token):
        yield token


def _session(status: str = "active", **values: Any) -> dict[str, Any]:
    return {
        "id": "lease-1",
        "source": "external_agent:codex:task1",
        "status": status,
        "reason": "Finish the assigned task",
        "consent_version": 1,
        "plugin_delta": [],
        "delta_version": 0,
        "applied_version": 0,
        **values,
    }


async def test_consent_carries_actual_source_and_survives_compaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        impersonation, "native_status", AsyncMock(return_value=_session("requested"))
    )
    first = await impersonation.claim_gate(BaseAgentState(), 42)
    assert first is not None
    assert first.goto == "before_llm"
    message = cast(HumanMessage, cast(dict[str, Any], first.update)["messages"][0])
    assert message.id == "impersonation-request:lease-1:1"
    content = cast(str, message.content)  # pyright: ignore[reportUnknownMemberType]
    assert "External agent" in content and "codex" in content
    assert "ava.impersonation.accept('lease-1')" in content
    compacted = BaseAgentState(impersonation_request_id="lease-1:1")
    assert await impersonation.claim_gate(compacted, 42) is None


@pytest.mark.parametrize("status", ["accepted", "active", "released", "expired"])
async def test_hold_ends_before_claim_or_compaction(
    monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    monkeypatch.setattr(impersonation, "native_status", AsyncMock(return_value=_session(status)))
    decision = await impersonation.claim_gate(BaseAgentState(), 42)
    assert decision is not None
    assert decision.goto == END
    hook = AsyncMock(return_value=Command(goto="llm"))
    guarded = impersonation.protect_native_hooks(hook)
    result = await guarded(
        BaseAgentState(),
        Runtime(context=AvaContext(ops_pool=MagicMock())),
        {"configurable": {"thread_id": "42"}},
    )
    assert result.goto == END
    hook.assert_not_awaited()


async def test_activation_waits_for_resource_closure(
    monkeypatch: pytest.MonkeyPatch, incarnation: RuntimeIncarnation
) -> None:
    monkeypatch.setattr(
        impersonation, "native_status", AsyncMock(return_value=_session("accepted"))
    )
    activate = Mock(return_value=_session())
    monkeypatch.setattr("shared.impersonation.activate", activate)
    monkeypatch.setattr(impersonation, "hosted_resources_settled", lambda: False)
    with pytest.raises(RuntimeError, match="unresolved native exec"):
        await impersonation.settle_checkpoint(MagicMock(), 42)
    activate.assert_not_called()
    assert not await impersonation.settle_checkpoint(MagicMock(), 42, activate_accepted=False)
    activate.assert_not_called()


async def test_checkpoint_receipt_prevents_reapplying_non_idempotent_delta(
    monkeypatch: pytest.MonkeyPatch, incarnation: RuntimeIncarnation
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

    def decode(delta: dict[str, Any]) -> dict[str, Any]:
        return delta

    monkeypatch.setattr("ava._external_state.decode_plugin_delta", decode)
    receipt = Mock(side_effect=RuntimeError("receipt commit lost"))
    monkeypatch.setattr("shared.impersonation.mark_plugin_applied", receipt)
    with pytest.raises(RuntimeError, match="receipt commit lost"):
        await impersonation.settle_checkpoint(graph, 42)
    assert (await graph.aget_state(config)).values["counter"] == 3
    receipt.side_effect = None
    await impersonation.settle_checkpoint(graph, 42)
    assert (await graph.aget_state(config)).values["counter"] == 3
    assert receipt.call_count == 2


def test_accept_stops_exec_and_uses_captured_incarnation(
    monkeypatch: pytest.MonkeyPatch, incarnation: RuntimeIncarnation
) -> None:
    from ava.impersonation import accept

    accepted = Mock()
    monkeypatch.setattr("shared.impersonation.accept", accepted)
    with pytest.raises(AgentImpersonation):
        accept("lease-1")
    accepted.assert_called_once_with("lease-1", 42, incarnation)
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
    from tests.conftest import spawn_agent

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
) -> None:

    from agent.db import claim_inbound_batch
    from agent.hosted_ownership import admit_hosted_runtime
    from shared.machine import machine_name
    from tests.conftest import spawn_agent

    agent_id = spawn_agent()
    owner = await admit_hosted_runtime(
        aops_pool, agent_id, machine_name(), uuid4(), expected_from="idling"
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
    from tests.conftest import spawn_agent

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


async def test_held_host_wake_returns_before_runtime_or_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.agent_host.host import AgentHost
    from services.agent_host.runtime import _StoredConfig

    host = object.__new__(AgentHost)
    host._machine = "local"
    host._owner = uuid4()
    host._maintenance_failed = {}
    host._control_pool = MagicMock()
    host._read_stored_config = AsyncMock(
        return_value=_StoredConfig(
            machine="local", status="idling", config_overlay=None, birth_config=None
        )
    )
    host._runtime_for = AsyncMock()
    monkeypatch.setattr("services.agent_host.host.active_lease", AsyncMock(return_value=True))
    monkeypatch.setattr("agent.db.claim_inbound_batch", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        "services.agent_host.host.apply_hosted_lifecycle", AsyncMock(return_value=None)
    )
    admission = AsyncMock(return_value=RuntimeIncarnation(42, uuid4(), host._owner))
    settlement = AsyncMock(return_value=True)
    monkeypatch.setattr("services.agent_host.host.admit_hosted_runtime", admission)
    monkeypatch.setattr("services.agent_host.host.settle_hosted_runtime", settlement)
    # No _turn_slots or graph exists: touching either is a test failure.
    await host._run_turn(42)
    host._runtime_for.assert_not_awaited()
    admission.assert_awaited_once()
    settlement.assert_awaited_once()


async def test_held_host_refuses_unaccepted_control_batch(monkeypatch: pytest.MonkeyPatch) -> None:
    from services.agent_host.host import AgentHost

    host = object.__new__(AgentHost)
    host._machine = "local"
    host._owner = uuid4()
    host._control_pool = MagicMock()
    owner = RuntimeIncarnation(42, uuid4(), host._owner)
    monkeypatch.setattr(
        "services.agent_host.host.admit_hosted_runtime", AsyncMock(return_value=owner)
    )
    monkeypatch.setattr(
        "agent.db.claim_inbound_batch",
        AsyncMock(return_value=[SimpleNamespace(durable_lifecycle=False)]),
    )
    apply = AsyncMock(return_value=None)
    monkeypatch.setattr("services.agent_host.host.apply_hosted_lifecycle", apply)
    monkeypatch.setattr("services.agent_host.host.settle_hosted_runtime", AsyncMock())
    with pytest.raises(RuntimeError, match="held control claim returned an unaccepted command"):
        await host._run_held_controls(42, "idling")
    apply.assert_not_awaited()
