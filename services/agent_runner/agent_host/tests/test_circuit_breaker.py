"""Permanent failures park background work; fresh input can request another try."""

import json
from datetime import UTC, datetime
from typing import Any, cast
from unittest.mock import MagicMock

import psycopg
import pytest
from langchain_core.messages import HumanMessage, SystemMessage
from psycopg_pool import AsyncConnectionPool

from agent.graph import claim_node
from agent.graph.llm_errors import FatalLLMStreamError, FatalProviderError
from agent.state import AgentState, CircuitState
from agent.tests.claim.claim_support import (
    _fake_llm,
    insert_inbound_kind,
    make_runtime,
    thread_config,
)
from agent.turn.runloop import _handle_fatal_llm_error
from base.agents.context import AvaContext
from base.cluster.machine import MachineIdentity
from base.config import settings
from base.config.service_read import ConfigAuthority
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.events.live.publisher import AgentEventPublisher
from base.host.env.agent_slices import AgentSlices
from base.lm.catalog import ModelCatalog
from base.lm.plugin_providers import build_model_catalog
from services.agent_runner.agent_host.tests.circuit_breaker.provider_failures import (
    LONG_SUMMARY,
)
from services.agent_runner.agent_host.tests.host_policy import configured_policy
from tests.fixtures.unit.identity import machine_identity as machine_identity
from tests.fixtures.units import spawn_agent


class _RecordingPublisher:
    """Minimal typed event sink for fatal-error live-event assertions."""

    def __init__(self) -> None:
        self.payloads: list[str] = []

    def emit(self, payload: str) -> None:
        self.payloads.append(payload)


def _overflow_state(breaker_reason: str | None = None) -> AgentState:
    """An agent state sitting past the provider's context ceiling, with the
    circuit breaker optionally open (the default closed)."""
    circuit = CircuitState()
    if breaker_reason is not None:
        circuit = CircuitState(
            open=True, reason=breaker_reason, opened_at="2026-08-29T00:00:00+00:00"
        )
    return AgentState(
        messages=[
            SystemMessage(content="<sys>"),
            *(HumanMessage(content=f"history-{i}") for i in range(10)),
        ],
        halted=True,
        circuit=circuit,
    )


def _breaker_ctx(*, database_gate: ProcessDbGate) -> AvaContext:
    """An AvaContext for `_handle_fatal_llm_error` — no ops_pool, so the
    best-effort event-log write is skipped (unit tests have no DB)."""
    return AvaContext(
        ops_pool=None,
        llm=MagicMock(),
        event_publisher=MagicMock(),
        agent=AgentSlices.resolve(default_reader=configured_policy().default_reader),
        db=Database.from_settings(gate=database_gate),
        bus=EventBus.from_settings(),
        catalog=build_model_catalog(),
        clock_factory=configured_policy().clock_factory,
    )


# ── breaker open (runloop `_handle_fatal_llm_error`) ──


async def test_fatal_provider_error_opens_circuit_breaker(
    loguru_records, *, database_gate: ProcessDbGate
) -> None:
    """A permanent context-overflow rejection opens the breaker with the
    context_overflow reason and keeps halted=True — the next wake must not
    re-fire the doomed call."""
    exc = FatalProviderError(
        "provider permanently rejected (HTTP 400): context length exceeded",
        error_class="permanent",
        provider="anthropic",
        status=400,
        context_overflow=True,
    )
    update = await _handle_fatal_llm_error(
        exc, _breaker_ctx(database_gate=database_gate), agent_id=42
    )

    assert update["halted"] is True
    circuit = update["circuit"]
    assert isinstance(circuit, CircuitState)
    assert circuit.open is True
    assert circuit.reason == "context_overflow"
    assert circuit.opened_at is not None
    records = [r for r in loguru_records if r["extra"].get("event") == "circuit_breaker_open"]  # pyright: ignore[reportUnknownMemberType]
    assert len(records) == 1  # pyright: ignore[reportUnknownArgumentType]
    assert records[0]["extra"]["reason"] == "context_overflow"


async def test_fatal_provider_error_billing_reason(*, database_gate: ProcessDbGate) -> None:
    """A 402 billing rejection opens the breaker too (heartbeat re-fires stop),
    but with the billing reason — no forced compact is armed for it."""
    exc = FatalProviderError(
        "provider rejected the request for billing (HTTP 402)",
        error_class="permanent",
        provider="anthropic",
        status=402,
    )
    update = await _handle_fatal_llm_error(
        exc, _breaker_ctx(database_gate=database_gate), agent_id=42
    )

    circuit = update["circuit"]
    assert isinstance(circuit, CircuitState)
    assert circuit.open is True
    assert circuit.reason == "billing"


async def test_fatal_provider_error_emits_blocked_recovery_details(
    *, database_gate: ProcessDbGate
) -> None:
    """The live error tells the user that a permanent rejection blocked retries.

    Regression for #5759: an opaque error plus an ``idling`` status made a
    permanent provider rejection look like an ordinary runnable idle state.
    """
    publisher = _RecordingPublisher()
    ctx = AvaContext(
        ops_pool=None,
        llm=MagicMock(),
        event_publisher=cast(AgentEventPublisher, publisher),
        agent=AgentSlices.resolve(default_reader=configured_policy().default_reader),
        db=Database.from_settings(gate=database_gate),
        bus=EventBus.from_settings(),
        catalog=build_model_catalog(),
        clock_factory=configured_policy().clock_factory,
    )
    exc = FatalProviderError(
        "provider permanently rejected (HTTP 400): Content Exists Risk",
        error_class="permanent",
        provider="anthropic",
        status=400,
    )

    await _handle_fatal_llm_error(exc, ctx, agent_id=42)

    emitted = json.loads(publisher.payloads[-1])
    assert emitted["role"] == "error"
    assert emitted["error_class"] == "permanent"
    assert emitted["reason"] == "bad_request"
    assert emitted["blocked"] is True
    assert (
        emitted["recovery"]
        == "Choose a different model overlay or resolve the provider policy rejection, then send a new message."
    )


async def test_permanent_provider_error_reports_metadata_to_nearest_alive_ancestor(
    redis_url: str,
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    """A blocked descendant reports only metadata through immutable SPAWN lineage.

    The immediate parent is terminated, so the report must skip it and reach
    the nearest live ancestor. The rejected provider body is deliberately
    distinctive: no history or error body may be replayed into the ancestor's
    prompt. The provider is the anthropic-compat error path while the pinned
    model makes the vendor the billed DeepSeek account: the report must name
    both, vendor first (task #3916).
    """
    ancestor_id = spawn_agent(
        spawner="user",
        catalog=model_catalog,
        authority=config_authority,
        database_gate=database_gate,
        machine=machine_identity,
    )
    terminated_parent_id = spawn_agent(
        spawner=f"agent:{ancestor_id}",
        catalog=model_catalog,
        authority=config_authority,
        database_gate=database_gate,
        machine=machine_identity,
    )
    child_id = spawn_agent(
        spawner=f"agent:{terminated_parent_id}",
        catalog=model_catalog,
        authority=config_authority,
        database_gate=database_gate,
        machine=machine_identity,
    )
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE agents_meta SET spawner = 'user', status = 'idling', "
            "lease_expires_at = now() + interval '10 minutes' WHERE id = %s",
            (ancestor_id,),
        )
        cur.execute(
            "UPDATE agents_meta SET spawner = %s, status = 'terminated' WHERE id = %s",
            (f"agent:{ancestor_id}", terminated_parent_id),
        )
        cur.execute(
            "UPDATE agents_meta SET spawner = %s WHERE id = %s",
            (f"agent:{terminated_parent_id}", child_id),
        )
    db_conn.commit()

    monkeypatch.setattr(settings.lm, "llm_model", "deepseek-flash")
    blocked_history = "Content Exists Risk: do not replay this rejected history"
    exc = FatalProviderError(
        blocked_history,
        error_class="permanent",
        provider="anthropic",
        status=400,
    )
    occurred_at = datetime(2026, 9, 3, 8, 0, tzinfo=UTC)
    await _handle_fatal_llm_error(
        exc,
        AvaContext(
            ops_pool=aops_pool,
            llm=MagicMock(),
            event_publisher=MagicMock(),
            agent=AgentSlices.resolve(default_reader=configured_policy().default_reader),
            db=Database.from_settings(gate=database_gate),
            bus=EventBus.from_settings(),
            catalog=model_catalog,
            clock_factory=configured_policy().clock_factory,
        ),
        agent_id=child_id,
        occurred_at=occurred_at,
    )

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT agent_id, content, kind, source, payload FROM inbound_messages "
            "WHERE kind = 'system_note' ORDER BY id"
        )
        rows = cur.fetchall()

    assert rows == [
        (
            ancestor_id,
            "Descendant agent "
            f"{child_id} is blocked after a permanent provider rejection. "
            "error_class=permanent vendor=deepseek provider=anthropic status=400 reason=bad_request "
            "timestamp=2026-09-03T08:00:00+00:00 "
            "where=agent.turn.runloop._handle_fatal_llm_error",
            "system_note",
            "system",
            {"note_tag": "agent_reply"},
        )
    ]
    assert blocked_history not in rows[0][1]


async def test_context_overflow_halts_background_wakes_and_reports_to_an_ancestor(
    redis_url: str,
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    """A permanent overflow rejection also halts unattended retries."""
    ancestor_id = spawn_agent(
        spawner="user",
        catalog=model_catalog,
        authority=config_authority,
        database_gate=database_gate,
        machine=machine_identity,
    )
    child_id = spawn_agent(
        spawner=f"agent:{ancestor_id}",
        catalog=model_catalog,
        authority=config_authority,
        database_gate=database_gate,
        machine=machine_identity,
    )
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE agents_meta SET status = 'idling', "
            "lease_expires_at = now() + interval '10 minutes' WHERE id = %s",
            (ancestor_id,),
        )
    db_conn.commit()

    await _handle_fatal_llm_error(
        FatalProviderError(
            "provider context window exceeded",
            error_class="permanent",
            provider="deepseek",
            status=400,
            context_overflow=True,
        ),
        AvaContext(
            ops_pool=aops_pool,
            llm=MagicMock(),
            event_publisher=MagicMock(),
            agent=AgentSlices.resolve(default_reader=configured_policy().default_reader),
            db=Database.from_settings(gate=database_gate),
            bus=EventBus.from_settings(),
            catalog=model_catalog,
            clock_factory=configured_policy().clock_factory,
        ),
        agent_id=child_id,
    )

    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM inbound_messages WHERE agent_id = %s", (ancestor_id,))
        assert cur.fetchone() == (1,)


async def test_fatal_llm_stream_error_does_not_open_breaker(
    *, database_gate: ProcessDbGate
) -> None:
    """FatalLLMStreamError (retry cap) is not a permanent provider rejection —
    it only halts the turn; the breaker stays untouched."""
    exc = FatalLLMStreamError("retry cap exhausted")
    update = await _handle_fatal_llm_error(
        exc, _breaker_ctx(database_gate=database_gate), agent_id=42
    )

    assert update == {"halted": True}


async def test_fatal_provider_error_does_not_reopen_already_open_breaker(
    *, database_gate: ProcessDbGate
) -> None:
    """A second failure while the breaker is already open for the same reason
    skips the duplicate open write + event (the original opened_at survives) —
    one open event per incident, not one per failed wake."""
    exc = FatalProviderError(
        "provider permanently rejected (HTTP 402)",
        error_class="permanent",
        provider="anthropic",
        status=402,
    )
    opened_at = "2026-08-29T00:00:00+00:00"

    async def _reader() -> CircuitState | None:
        return CircuitState(open=True, reason="billing", opened_at=opened_at)

    update = await _handle_fatal_llm_error(
        exc, _breaker_ctx(database_gate=database_gate), agent_id=42, circuit_reader=_reader
    )

    assert update == {"halted": True}, (
        "the breaker is already open — the duplicate write must be skipped"
    )


# ── heartbeat gating (claim node) ──


@pytest.mark.parametrize("reason", ["billing", "context_overflow"])
async def test_heartbeat_while_breaker_open_parks(
    redis_url: str,
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    reason: str,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    """Every permanent rejection blocks background model requests: the heartbeat is
    consumed without a note and parks at claim — no LLM call, no compact, no
    doomed re-fire."""
    tid = spawn_agent(
        catalog=model_catalog,
        authority=config_authority,
        database_gate=database_gate,
        machine=machine_identity,
    )
    insert_inbound_kind(db_conn, tid, "Heartbeat.", "heartbeat")

    state = _overflow_state(breaker_reason=reason)
    fake_llm = _fake_llm(LONG_SUMMARY)
    cmd = await claim_node(
        state,
        make_runtime(ops_pool=aops_pool, llm=fake_llm, database_gate=database_gate),
        thread_config(tid),
    )

    fake_llm.bind_tools.return_value.ainvoke.assert_not_called()
    assert not cmd.update.get("messages"), "no note appended while breaker open"  # pyright: ignore[reportOptionalMemberAccess]
    assert cmd.goto == "claim"


@pytest.mark.parametrize(
    ("first_kind", "second_kind"),
    [("chat", "heartbeat"), ("heartbeat", "chat")],
)
async def test_chat_cobatched_with_open_breaker_heartbeat_reaches_llm(
    redis_url: str,
    first_kind: str,
    second_kind: str,
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    """A parked heartbeat must not bury a same-batch chat in either FIFO order."""
    tid = spawn_agent(
        catalog=model_catalog,
        authority=config_authority,
        database_gate=database_gate,
        machine=machine_identity,
    )
    inbound_ids: dict[str, int] = {}
    for kind in (first_kind, second_kind):
        content = "real user work" if kind == "chat" else "Heartbeat."
        inbound_ids[kind] = insert_inbound_kind(db_conn, tid, content, kind, source="user")

    fake_llm = _fake_llm(LONG_SUMMARY)
    cmd = await claim_node(
        _overflow_state(breaker_reason="billing"),
        make_runtime(ops_pool=aops_pool, llm=fake_llm, database_gate=database_gate),
        thread_config(tid),
    )

    assert cmd.goto == "before_llm"
    assert cmd.update["halted"] is False  # pyright: ignore[reportOptionalSubscript, reportUnknownArgumentType]
    messages = cmd.update["messages"]  # pyright: ignore[reportOptionalSubscript, reportUnknownArgumentType]
    assert len(messages) == 1  # pyright: ignore[reportUnknownArgumentType]
    assert isinstance(messages[0], HumanMessage)
    assert "real user work" in messages[0].content  # pyright: ignore[reportUnknownMemberType]
    assert messages[0].additional_kwargs["ava_inbound_id"] == inbound_ids["chat"]  # pyright: ignore[reportUnknownMemberType]
    assert "Heartbeat." not in messages[0].content  # pyright: ignore[reportUnknownMemberType]
    fake_llm.bind_tools.return_value.ainvoke.assert_not_called()


@pytest.mark.parametrize("reason", ["billing", "context_overflow"])
async def test_claim_parks_idle_while_breaker_open(
    redis_url: str,
    aops_pool: AsyncConnectionPool,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    reason: str,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    """The claim no-batch branch parks every open breaker: a
    self-initiated continue-loop (the next graph invocation after the turn
    boundary) must not re-fire the doomed call. Hosted mode surfaces the park
    as END+turn_idle without blocking on the inbound wait."""
    tid = spawn_agent(
        catalog=model_catalog,
        authority=config_authority,
        database_gate=database_gate,
        machine=machine_identity,
    )
    state = AgentState(
        messages=[SystemMessage(content="<sys>"), HumanMessage(content="hi")],
        halted=False,
        circuit=CircuitState(open=True, reason=reason, opened_at="2026-08-29T00:00:00+00:00"),
    )
    cmd = await claim_node(
        state,
        make_runtime(ops_pool=aops_pool, database_gate=database_gate),
        thread_config(tid),
    )

    assert cmd.goto == "__end__"
    assert cmd.update["turn_idle"] is True  # pyright: ignore[reportOptionalSubscript, reportUnknownMemberType]


async def test_claim_does_not_park_while_breaker_closed(
    redis_url: str,
    aops_pool: AsyncConnectionPool,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    """Control: with the breaker closed the same no-batch state routes to the
    LLM as before (the continue-working path)."""
    tid = spawn_agent(
        catalog=model_catalog,
        authority=config_authority,
        database_gate=database_gate,
        machine=machine_identity,
    )
    state = AgentState(
        messages=[SystemMessage(content="<sys>"), HumanMessage(content="hi")],
        halted=False,
    )
    cmd = await claim_node(
        state,
        make_runtime(ops_pool=aops_pool, database_gate=database_gate),
        thread_config(tid),
    )

    assert cmd.goto == "before_llm"


async def test_heartbeat_normal_when_breaker_closed(
    redis_url: str,
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    """Breaker closed: the heartbeat check-in note is appended and the wake
    routes to the LLM as before — the gate only exists while the breaker is
    open."""
    tid = spawn_agent(
        catalog=model_catalog,
        authority=config_authority,
        database_gate=database_gate,
        machine=machine_identity,
    )
    insert_inbound_kind(db_conn, tid, "Heartbeat.", "heartbeat")

    state = _overflow_state()  # breaker closed
    fake_llm = _fake_llm(LONG_SUMMARY)
    cmd = await claim_node(
        state,
        make_runtime(ops_pool=aops_pool, llm=fake_llm, database_gate=database_gate),
        thread_config(tid),
    )

    fake_llm.bind_tools.return_value.ainvoke.assert_not_called()
    msgs = cmd.update["messages"]  # pyright: ignore[reportOptionalSubscript, reportUnknownMemberType]
    assert len(msgs) == 1  # pyright: ignore[reportUnknownArgumentType]
    assert "Heartbeat." in msgs[0].content  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
    assert cmd.goto == "before_llm"


# ── breaker close (llm node) ──


# ── recovery circuit breaker (task #3617) ────────────────────────────────────


def _permanent_rejection() -> FatalProviderError:
    """A permanent 400 in the 6260 shape; the body is deliberately distinctive
    so the tests can prove no rejected text reaches the ancestor report."""
    return FatalProviderError(
        "Content Exists Risk: do not replay this rejected history",
        error_class="permanent",
        provider="deepseek",
        status=400,
    )


async def _reject_turn(
    aops_pool: AsyncConnectionPool,
    agent_id: int,
    *,
    publisher: _RecordingPublisher,
    exc: FatalProviderError | None = None,
    database_gate: ProcessDbGate,
) -> None:
    await _handle_fatal_llm_error(
        exc if exc is not None else _permanent_rejection(),
        AvaContext(
            ops_pool=aops_pool,
            llm=MagicMock(),
            event_publisher=cast(AgentEventPublisher, publisher),
            agent=AgentSlices.resolve(default_reader=configured_policy().default_reader),
            db=Database.from_settings(gate=database_gate),
            bus=EventBus.from_settings(),
            catalog=build_model_catalog(),
            clock_factory=configured_policy().clock_factory,
        ),
        agent_id=agent_id,
        occurred_at=datetime(2026, 9, 16, 6, 0, tzinfo=UTC),
    )


def _spawn_child_under_idling_ancestor(
    db_conn: psycopg.Connection,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    local_machine: MachineIdentity,
) -> tuple[int, int]:
    """A live idling ancestor and its child; returns (ancestor_id, child_id)."""
    ancestor_id = spawn_agent(
        spawner="user",
        catalog=model_catalog,
        authority=config_authority,
        database_gate=database_gate,
        machine=local_machine,
    )
    child_id = spawn_agent(
        spawner=f"agent:{ancestor_id}",
        catalog=model_catalog,
        authority=config_authority,
        database_gate=database_gate,
        machine=local_machine,
    )
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE agents_meta SET status = 'idling', "
            "lease_expires_at = now() + interval '10 minutes' WHERE id = %s",
            (ancestor_id,),
        )
    db_conn.commit()
    return ancestor_id, child_id


def _ancestor_halt_notes(db_conn: psycopg.Connection, ancestor_id: int) -> list[str]:
    reports = [
        r[0]
        for r in db_conn.execute(
            "SELECT content FROM inbound_messages WHERE agent_id = %s AND kind = 'system_note' "
            "ORDER BY id",
            (ancestor_id,),
        ).fetchall()
    ]
    return [note for note in reports if "blocked after a permanent provider rejection" in note]


def _recovery_halt_errors(publisher: _RecordingPublisher) -> list[dict[str, Any]]:
    errors = [json.loads(payload) for payload in publisher.payloads]
    return [
        err
        for err in errors
        if err.get("blocked") and "Automatic recovery is halted" in err.get("content", "")
    ]
