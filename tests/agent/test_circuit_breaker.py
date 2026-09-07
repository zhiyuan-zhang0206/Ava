"""Heartbeat circuit breaker + overflow self-rescue tests (Task #1928).

Locks the two framework-side fixes for the 3962 context-overflow incident (80
heartbeat cycles against a permanent context-overflow 400, no self-rescue):

1. **breaker open** — a `FatalProviderError` (permanent provider rejection)
   opens the `circuit` channel; heartbeat check-ins are then consumed without
   routing to the doomed LLM call (`_handle_heartbeat`), the claim node parks
   idle instead of continue-looping (`circuit.parks_idle`), and for the
   `context_overflow` reason any wake forces a compaction instead
   (`decide` → `emergency_compact_summary`).
2. **overflow self-rescue** — `emergency_compact_summary` tries a real
   compaction, then falls back to the no-LLM minimal compact when the
   compaction request itself is permanently rejected; the breaker closes on
   the first successful LLM call (`llm_node`).
"""

import json
from datetime import UTC, datetime
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import psycopg
import pytest
from langchain_core.messages import AIMessageChunk, AnyMessage, HumanMessage, SystemMessage
from psycopg_pool import AsyncConnectionPool

from agent._runloop import _handle_fatal_llm_error
from agent.graph import claim_node, llm_node
from agent.graph._context import AvaContext
from agent.graph._llm import FatalLLMStreamError, FatalProviderError
from agent.hooks.compact import (
    _EMERGENCY_COMPACT_MARKER,
    COMPACT_MAX_ATTEMPTS,
    CompactionFailedError,
    compose_summary_message,
    emergency_compact_summary,
)
from agent.state import AgentState, CircuitState
from shared.event_publisher import AgentEventPublisher
from tests.agent.test_claim import (
    _compact_tail,
    _config,
    _fake_llm,
    _insert_inbound_kind,
    _make_runtime,
)
from tests.agent.test_llm_helpers import _CONFIG as _LLM_CONFIG
from tests.agent.test_llm_helpers import _make_runtime as _llm_make_runtime
from tests.conftest import spawn_agent

# A summary long enough to clear COMPACT_MIN_SUMMARY_CHARS.
_LONG_SUMMARY = "## Requests\nfollow the template. " * 60


class _FakeProviderStatusError(Exception):
    """anthropic/openai APIStatusError shape driving the classifier."""

    def __init__(self, status_code: int, body: dict | None = None) -> None:
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code
        self.body = body  # pyright: ignore[reportUnknownMemberType]


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


def _breaker_ctx() -> AvaContext:
    """An AvaContext for `_handle_fatal_llm_error` — no ops_pool, so the
    best-effort event-log write is skipped (unit tests have no DB)."""
    return AvaContext(ops_pool=None, llm=MagicMock(), event_publisher=MagicMock())


# ── breaker open (runloop `_handle_fatal_llm_error`) ──


async def test_fatal_provider_error_opens_circuit_breaker(loguru_records) -> None:
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
    update = await _handle_fatal_llm_error(exc, _breaker_ctx(), agent_id=42)

    assert update["halted"] is True
    circuit = update["circuit"]
    assert isinstance(circuit, CircuitState)
    assert circuit.open is True
    assert circuit.reason == "context_overflow"
    assert circuit.opened_at is not None
    records = [r for r in loguru_records if r["extra"].get("event") == "circuit_breaker_open"]  # pyright: ignore[reportUnknownMemberType]
    assert len(records) == 1  # pyright: ignore[reportUnknownArgumentType]
    assert records[0]["extra"]["reason"] == "context_overflow"


async def test_fatal_provider_error_billing_reason() -> None:
    """A 402 billing rejection opens the breaker too (heartbeat re-fires stop),
    but with the billing reason — no forced compact is armed for it."""
    exc = FatalProviderError(
        "provider rejected the request for billing (HTTP 402)",
        error_class="permanent",
        provider="anthropic",
        status=402,
    )
    update = await _handle_fatal_llm_error(exc, _breaker_ctx(), agent_id=42)

    circuit = update["circuit"]
    assert isinstance(circuit, CircuitState)
    assert circuit.open is True
    assert circuit.reason == "billing"


async def test_fatal_provider_error_emits_blocked_recovery_details() -> None:
    """The live error tells the user that a permanent rejection blocked retries.

    Regression for #5759: an opaque error plus an ``idling`` status made a
    permanent provider rejection look like an ordinary runnable idle state.
    """
    publisher = _RecordingPublisher()
    ctx = AvaContext(
        ops_pool=None,
        llm=MagicMock(),
        event_publisher=cast(AgentEventPublisher, publisher),
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
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
) -> None:
    """A blocked descendant reports only metadata through immutable SPAWN lineage.

    The immediate parent is terminated, so the report must skip it and reach
    the nearest live ancestor. The rejected provider body is deliberately
    distinctive: no history or error body may be replayed into the ancestor's
    prompt.
    """
    ancestor_id = spawn_agent(spawner="user")
    terminated_parent_id = spawn_agent(spawner=f"agent:{ancestor_id}")
    child_id = spawn_agent(spawner=f"agent:{terminated_parent_id}")
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

    blocked_history = "Content Exists Risk: do not replay this rejected history"
    exc = FatalProviderError(
        blocked_history,
        error_class="permanent",
        provider="deepseek",
        status=400,
    )
    occurred_at = datetime(2026, 9, 3, 8, 0, tzinfo=UTC)
    await _handle_fatal_llm_error(
        exc,
        AvaContext(ops_pool=aops_pool, llm=MagicMock(), event_publisher=MagicMock()),
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
            "error_class=permanent provider=deepseek status=400 reason=bad_request "
            "timestamp=2026-09-03T08:00:00+00:00 "
            "where=agent._runloop._handle_fatal_llm_error",
            "system_note",
            "system",
            {"note_tag": "agent_reply"},
        )
    ]
    assert blocked_history not in rows[0][1]


async def test_context_overflow_self_recovery_does_not_report_to_an_ancestor(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
) -> None:
    """Forced compaction is a healthy recovery path, not an ancestor escalation."""
    ancestor_id = spawn_agent(spawner="user")
    child_id = spawn_agent(spawner=f"agent:{ancestor_id}")
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
        AvaContext(ops_pool=aops_pool, llm=MagicMock(), event_publisher=MagicMock()),
        agent_id=child_id,
    )

    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM inbound_messages WHERE agent_id = %s", (ancestor_id,))
        assert cur.fetchone() == (0,)


async def test_fatal_llm_stream_error_does_not_open_breaker() -> None:
    """FatalLLMStreamError (retry cap) is not a permanent provider rejection —
    it only halts the turn; the breaker stays untouched."""
    exc = FatalLLMStreamError("retry cap exhausted")
    update = await _handle_fatal_llm_error(exc, _breaker_ctx(), agent_id=42)

    assert update == {"halted": True}


async def test_fatal_provider_error_does_not_reopen_already_open_breaker() -> None:
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

    update = await _handle_fatal_llm_error(exc, _breaker_ctx(), agent_id=42, circuit_reader=_reader)

    assert update == {"halted": True}, (
        "the breaker is already open — the duplicate write must be skipped"
    )


# ── heartbeat gating (claim node) ──


async def test_heartbeat_while_breaker_open_forces_compact(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
) -> None:
    """Breaker open with context_overflow + heartbeat wake → the check-in note
    is NOT appended (no doomed call), and the wake routes into a compaction
    whose tail is the generated summary — the overflow self-rescue."""
    tid = spawn_agent()
    _insert_inbound_kind(db_conn, tid, "Heartbeat.", "heartbeat")

    state = _overflow_state(breaker_reason="context_overflow")
    fake_llm = _fake_llm(_LONG_SUMMARY)
    cmd = await claim_node(
        state,
        _make_runtime(
            ops_pool=aops_pool,
            llm=fake_llm,
        ),
        _config(tid),
    )

    fake_llm.bind_tools.return_value.ainvoke.assert_called_once()  # the compaction call
    tail = _compact_tail(cmd.update)
    assert len(tail) == 1, "forced compact tail must be the summary alone — no heartbeat note"  # pyright: ignore[reportUnknownArgumentType]
    assert tail[0].content == compose_summary_message(_LONG_SUMMARY)  # pyright: ignore[reportUnknownMemberType]
    assert cmd.update["compact"].version == 1  # pyright: ignore[reportOptionalSubscript, reportUnknownArgumentType, reportUnknownMemberType]


async def test_heartbeat_while_breaker_open_falls_back_to_minimal_compact(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
) -> None:
    """The 3962 shape: the compaction request itself is rejected (context over
    the effective input ceiling) — the wake must still be rescued by the
    no-LLM minimal compact instead of looping forever."""
    tid = spawn_agent()
    _insert_inbound_kind(db_conn, tid, "Heartbeat.", "heartbeat")

    state = _overflow_state(breaker_reason="context_overflow")
    llm = MagicMock()
    llm.bind_tools.return_value.ainvoke = AsyncMock(
        side_effect=_FakeProviderStatusError(
            400,
            {"error": {"type": "invalid_request_error", "message": "maximum context length"}},
        )
    )
    cmd = await claim_node(
        state,
        _make_runtime(
            ops_pool=aops_pool,
            llm=llm,
        ),
        _config(tid),
    )

    tail = _compact_tail(cmd.update)
    assert _EMERGENCY_COMPACT_MARKER in tail[0].content  # pyright: ignore[reportUnknownMemberType]


async def test_heartbeat_while_breaker_open_non_overflow_parks(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
) -> None:
    """Breaker open with a non-overflow reason (billing): the heartbeat is
    consumed without a note and parks at claim — no LLM call, no compact, no
    doomed re-fire."""
    tid = spawn_agent()
    _insert_inbound_kind(db_conn, tid, "Heartbeat.", "heartbeat")

    state = _overflow_state(breaker_reason="billing")
    fake_llm = _fake_llm(_LONG_SUMMARY)
    cmd = await claim_node(
        state,
        _make_runtime(
            ops_pool=aops_pool,
            llm=fake_llm,
        ),
        _config(tid),
    )

    fake_llm.bind_tools.return_value.ainvoke.assert_not_called()
    assert not cmd.update.get("messages"), "no note appended while breaker open"  # pyright: ignore[reportOptionalMemberAccess]
    assert cmd.goto == "claim"


@pytest.mark.parametrize(
    ("first_kind", "second_kind"),
    [("chat", "heartbeat"), ("heartbeat", "chat")],
)
async def test_chat_cobatched_with_open_breaker_heartbeat_reaches_llm(
    first_kind: str,
    second_kind: str,
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
) -> None:
    """A parked heartbeat must not bury a same-batch chat in either FIFO order."""
    tid = spawn_agent()
    inbound_ids: dict[str, int] = {}
    for kind in (first_kind, second_kind):
        content = "real user work" if kind == "chat" else "Heartbeat."
        inbound_ids[kind] = _insert_inbound_kind(db_conn, tid, content, kind, source="user")

    fake_llm = _fake_llm(_LONG_SUMMARY)
    cmd = await claim_node(
        _overflow_state(breaker_reason="billing"),
        _make_runtime(
            ops_pool=aops_pool,
            llm=fake_llm,
        ),
        _config(tid),
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


async def test_claim_parks_idle_while_non_overflow_breaker_open(
    aops_pool: AsyncConnectionPool,
) -> None:
    """The claim no-batch branch parks a non-overflow open breaker: a
    self-initiated continue-loop (the next graph invocation after the turn
    boundary) must not re-fire the doomed call. Hosted mode surfaces the park
    as END+turn_idle without blocking on the inbound wait."""
    tid = spawn_agent()
    state = AgentState(
        messages=[SystemMessage(content="<sys>"), HumanMessage(content="hi")],
        halted=False,
        circuit=CircuitState(open=True, reason="billing", opened_at="2026-08-29T00:00:00+00:00"),
    )
    cmd = await claim_node(
        state,
        _make_runtime(ops_pool=aops_pool),
        _config(tid),
    )

    assert cmd.goto == "__end__"
    assert cmd.update["turn_idle"] is True  # pyright: ignore[reportOptionalSubscript, reportUnknownMemberType]


async def test_claim_does_not_park_while_breaker_closed(
    aops_pool: AsyncConnectionPool,
) -> None:
    """Control: with the breaker closed the same no-batch state routes to the
    LLM as before (the continue-working path)."""
    tid = spawn_agent()
    state = AgentState(
        messages=[SystemMessage(content="<sys>"), HumanMessage(content="hi")],
        halted=False,
    )
    cmd = await claim_node(
        state,
        _make_runtime(ops_pool=aops_pool),
        _config(tid),
    )

    assert cmd.goto == "before_llm"


async def test_heartbeat_normal_when_breaker_closed(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
) -> None:
    """Breaker closed: the heartbeat check-in note is appended and the wake
    routes to the LLM as before — the gate only exists while the breaker is
    open."""
    tid = spawn_agent()
    _insert_inbound_kind(db_conn, tid, "Heartbeat.", "heartbeat")

    state = _overflow_state()  # breaker closed
    fake_llm = _fake_llm(_LONG_SUMMARY)
    cmd = await claim_node(
        state,
        _make_runtime(
            ops_pool=aops_pool,
            llm=fake_llm,
        ),
        _config(tid),
    )

    fake_llm.bind_tools.return_value.ainvoke.assert_not_called()
    msgs = cmd.update["messages"]  # pyright: ignore[reportOptionalSubscript, reportUnknownMemberType]
    assert len(msgs) == 1  # pyright: ignore[reportUnknownArgumentType]
    assert "Heartbeat." in msgs[0].content  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
    assert cmd.goto == "before_llm"


# ── emergency_compact_summary (unit) ──


async def test_emergency_compact_summary_uses_real_summary() -> None:
    """The compaction call succeeds → its summary is used (the no-LLM fallback
    only fires when the request cannot go out)."""
    msgs: list[AnyMessage] = [SystemMessage(content="<sys>"), HumanMessage(content="hi")]
    summary = await emergency_compact_summary(msgs, _fake_llm(_LONG_SUMMARY))
    assert summary == _LONG_SUMMARY


async def test_emergency_compact_summary_falls_back_on_permanent_rejection() -> None:
    """Every compaction attempt is permanently rejected → the marker fallback
    is returned instead of raising — the wipe still happens, the agent is
    rescued without any model call."""
    msgs: list[AnyMessage] = [SystemMessage(content="<sys>"), HumanMessage(content="hi")]
    llm = MagicMock()
    llm.bind_tools.return_value.ainvoke = AsyncMock(
        side_effect=_FakeProviderStatusError(
            400,
            {"error": {"type": "invalid_request_error", "message": "maximum context length"}},
        )
    )

    summary = await emergency_compact_summary(msgs, llm)
    assert _EMERGENCY_COMPACT_MARKER in summary
    assert llm.bind_tools.return_value.ainvoke.await_count == 1, (
        "a permanent rejection must not be retried — the request cannot succeed"
    )


async def test_emergency_compact_summary_preserves_last_prior_summary() -> None:
    """The fallback embeds the last preserved compaction summary, so the
    model-less wipe keeps as much memory as possible."""
    prior = "## Requests\nremember the prior compact. " * 40
    msgs: list[AnyMessage] = [
        SystemMessage(content="<sys>"),
        HumanMessage(
            content=compose_summary_message(prior),
            additional_kwargs={"ava_msg_type": "compact_summary"},
        ),
        HumanMessage(content="work since the last compact"),
    ]
    llm = MagicMock()
    llm.bind_tools.return_value.ainvoke = AsyncMock(side_effect=_FakeProviderStatusError(400))

    summary = await emergency_compact_summary(msgs, llm)
    assert prior in summary
    assert _EMERGENCY_COMPACT_MARKER in summary


async def test_emergency_compact_summary_raises_on_transient_exhaustion() -> None:
    """A transient failure (provider 502) is retried and, exhausted, raises
    CompactionFailedError — a provider blip must not silently destroy the
    conversation with the wipe fallback."""
    msgs: list[AnyMessage] = [SystemMessage(content="<sys>"), HumanMessage(content="hi")]
    llm = MagicMock()
    llm.bind_tools.return_value.ainvoke = AsyncMock(side_effect=RuntimeError("provider 502"))

    with pytest.raises(CompactionFailedError, match="no usable summary"):
        await emergency_compact_summary(msgs, llm)
    assert llm.bind_tools.return_value.ainvoke.await_count == COMPACT_MAX_ATTEMPTS


# ── breaker close (llm node) ──


async def test_llm_node_closes_circuit_on_success() -> None:
    """A successful LLM call is the circuit-healed signal — the breaker closes
    so heartbeats resume routing normally."""

    async def _fast_complete() -> Any:
        yield AIMessageChunk(
            content="hi",
            response_metadata={"model_provider": "anthropic", "stop_reason": "end_turn"},
            usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        )

    fake_llm = MagicMock()
    fake_llm.astream.return_value = _fast_complete()
    state = _overflow_state(breaker_reason="context_overflow")

    cmd = await llm_node(
        state,
        _llm_make_runtime(llm=fake_llm, event_publisher=MagicMock()),
        _LLM_CONFIG,
    )

    assert cmd.update["circuit"].open is False  # pyright: ignore[reportOptionalSubscript, reportUnknownMemberType]
    assert cmd.update["circuit"].reason is None  # pyright: ignore[reportOptionalSubscript, reportUnknownMemberType]


async def test_llm_node_cancel_does_not_close_circuit(fake_cancel_event) -> None:
    """The cancel path discards the partial generation — no stream completed,
    so the breaker must stay open (closing it without a healed call would
    re-arm the doomed heartbeat calls)."""
    import asyncio

    async def _stream_then_hang() -> Any:
        yield AIMessageChunk(content="partial")
        await asyncio.Future()  # hang until cancelled

    fake_llm = MagicMock()
    fake_llm.astream.return_value = _stream_then_hang()
    state = _overflow_state(breaker_reason="billing")

    async def _trigger() -> None:
        await asyncio.sleep(0.05)
        fake_cancel_event.set()  # pyright: ignore[reportUnknownMemberType]

    trigger = asyncio.create_task(_trigger())
    cmd = await llm_node(
        state,
        _llm_make_runtime(llm=fake_llm, event_publisher=MagicMock()),
        _LLM_CONFIG,
    )
    await trigger

    assert cmd.update.get("circuit") is None, "cancel path must not close the breaker"  # pyright: ignore[reportOptionalMemberAccess]
    assert cmd.update["halted"] is True  # pyright: ignore[reportOptionalSubscript, reportUnknownMemberType]


@pytest.mark.parametrize("overflow", [False, True])
async def test_host_persists_provider_failure_before_releasing_turn(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    overflow: bool,
) -> None:
    """A real graph failure is flushed to PG; a fresh reader sees its breaker."""
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from langgraph.graph import END, START, StateGraph

    from agent.startup import _wrap_saver_writes_with_nstep_interval
    from services.agent_host.host import AgentHost
    from shared.config import settings

    agent_id = spawn_agent()
    calls = 0

    def reject(state: AgentState) -> dict[str, object]:
        nonlocal calls
        calls += 1
        raise FatalProviderError(
            "synthetic provider refusal",
            error_class="permanent",
            provider="anthropic",
            status=400 if overflow else 402,
            context_overflow=overflow,
        )

    builder = StateGraph(AgentState, context_schema=AvaContext)
    builder.add_node("reject", reject)  # pyright: ignore[reportUnknownMemberType]
    builder.add_edge(START, "reject")
    builder.add_edge("reject", END)
    async with AsyncPostgresSaver.from_conn_string(settings.data_plane.db_url) as saver:
        _wrap_saver_writes_with_nstep_interval(saver, 100)
        graph = builder.compile(checkpointer=saver)  # pyright: ignore[reportUnknownMemberType]
        host = AgentHost(pool=aops_pool, checkpointer=saver, graph=graph, machine="test")
        assert not await host._invoke_until_done(agent_id, _breaker_ctx())
    # New saver/connection prevents in-memory buffered state from faking success.
    async with AsyncPostgresSaver.from_conn_string(settings.data_plane.db_url) as reader:
        stored = await reader.aget_tuple({"configurable": {"thread_id": str(agent_id)}})
    assert stored is not None
    values = stored.checkpoint["channel_values"]
    assert values["halted"] is True
    circuit = CircuitState.model_validate(values["circuit"])
    assert circuit.open is True
    assert circuit.reason == ("context_overflow" if overflow else "billing")
    assert circuit.opened_at is not None
    assert calls == 1
