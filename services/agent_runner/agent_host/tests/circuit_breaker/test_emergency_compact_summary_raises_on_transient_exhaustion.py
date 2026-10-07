# pyright: reportOptionalSubscript=false
"""Circuit breaker cases: emergency compact summary raises on transient exhaustion."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import psycopg
import pytest
from langchain_core.messages import AIMessageChunk, AnyMessage, HumanMessage, SystemMessage
from psycopg_pool import AsyncConnectionPool

from agent.graph import llm_node
from agent.graph.llm_errors import FatalProviderError, LlmLedger
from agent.graph.tests.test_llm_helpers import _CONFIG as _LLM_CONFIG
from agent.graph.tests.test_llm_helpers import _make_runtime as _llm_make_runtime
from agent.hooks.compact import (
    COMPACT_MAX_ATTEMPTS,
    CompactionFailedError,
    emergency_compact_summary,
)
from base.agents.context import AvaContext
from base.db import Database
from base.events.live.bus import EventBus
from base.host.env.agent_slices import AgentSlices
from services.agent_runner.agent_host.tests.test_circuit_breaker import (
    _ancestor_halt_notes,
    _overflow_state,
    _RecordingPublisher,
    _recovery_halt_errors,
    _reject_turn,
    _spawn_child_under_idling_ancestor,
)
from tests.fixtures.units import spawn_agent


async def test_emergency_compact_summary_raises_on_transient_exhaustion() -> None:
    """A transient failure (provider 502) is retried and, exhausted, raises
    CompactionFailedError — a provider blip must not silently destroy the
    conversation with the wipe fallback."""
    msgs: list[AnyMessage] = [SystemMessage(content="<sys>"), HumanMessage(content="hi")]
    llm = MagicMock()
    llm.bind_tools.return_value.ainvoke = AsyncMock(side_effect=RuntimeError("provider 502"))

    with pytest.raises(CompactionFailedError, match="no usable summary"):
        await emergency_compact_summary(msgs, llm, AgentSlices.resolve())
    assert llm.bind_tools.return_value.ainvoke.await_count == COMPACT_MAX_ATTEMPTS


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
        ledger=LlmLedger(),
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
        ledger=LlmLedger(),
    )
    await trigger

    assert cmd.update.get("circuit") is None, "cancel path must not close the breaker"  # pyright: ignore[reportOptionalMemberAccess]
    assert cmd.update["halted"] is True  # pyright: ignore[reportOptionalSubscript, reportUnknownMemberType]


async def test_two_permanent_rejections_trip_the_recovery_breaker(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    loguru_records: list[dict[str, Any]],
) -> None:
    """Two consecutive permanent rejections with no successful turn between
    them halt automatic recovery: the durable streak reaches the threshold,
    the wake suppression names the reason, the metadata-only report reaches
    the nearest live ancestor, and the frontend gets a blocked Error — while
    ONE rejection alone changes none of it."""
    ancestor_id, child_id = _spawn_child_under_idling_ancestor(db_conn)
    publisher = _RecordingPublisher()

    await _reject_turn(aops_pool, child_id, publisher=publisher)
    row = db_conn.execute(
        "SELECT permanent_reject_streak, wake_suppress_reason FROM agents_meta WHERE id = %s",
        (child_id,),
    ).fetchone()
    assert row == (1, None)  # one rejection is not a halt

    await _reject_turn(aops_pool, child_id, publisher=publisher)
    row = db_conn.execute(
        "SELECT permanent_reject_streak, wake_suppress_reason, "
        "EXTRACT(EPOCH FROM (wake_suppressed_until - clock_timestamp())) "
        "FROM agents_meta WHERE id = %s",
        (child_id,),
    ).fetchone()
    assert row is not None
    streak, reason, window_s = row
    assert streak == 2
    assert reason == "permanent_provider_reject"
    assert window_s > 300 * 24 * 3600.0  # until-human, far past any timer

    halt_notes = _ancestor_halt_notes(db_conn, ancestor_id)
    assert len(halt_notes) == 1
    assert "do not replay" not in halt_notes[0]  # metadata only, never the text

    halt_logs = [
        r
        for r in loguru_records
        if r["extra"].get("event") == "recovery_breaker_halt"  # pyright: ignore[reportUnknownMemberType]
    ]
    assert len(halt_logs) == 1
    assert len(_recovery_halt_errors(publisher)) == 1


async def test_transient_rejection_does_not_count_or_trip(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
) -> None:
    """Only the PERMANENT class counts: a configured-fatal/transient rejection
    aborts the turn but never arms the recovery breaker."""
    child_id = spawn_agent(spawner="user")
    exc = FatalProviderError(
        "rate limited", error_class="transient", provider="deepseek", status=429
    )
    for _ in range(3):
        await _reject_turn(aops_pool, child_id, publisher=_RecordingPublisher(), exc=exc)
    row = db_conn.execute(
        "SELECT permanent_reject_streak, wake_suppress_reason FROM agents_meta WHERE id = %s",
        (child_id,),
    ).fetchone()
    assert row == (0, None)


async def test_completed_turn_resets_the_streak_and_clears_the_marker(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
) -> None:
    """The completed-turn UPDATE is the single reset: it clears the corpse
    marker, the recovery-breaker streak, and the recorded reject reason
    together (agent/graph/llm/node.py)."""
    from agent.graph.llm.node import _persist_last_active

    child_id = spawn_agent(spawner="user")
    db_conn.execute(
        "UPDATE agents_meta SET permanent_reject_streak = 2, "
        "last_permanent_reject_reason = 'billing', last_turn_fatal_at = now() "
        "WHERE id = %s",
        (child_id,),
    )
    db_conn.commit()

    await _persist_last_active(
        AvaContext(
            ops_pool=aops_pool,
            llm=MagicMock(),
            event_publisher=MagicMock(),
            agent=AgentSlices.resolve(),
            db=Database.from_settings(),
            bus=EventBus.from_settings(),
        ),
        child_id,
        "done",
    )
    row = db_conn.execute(
        "SELECT permanent_reject_streak, last_turn_fatal_at, last_permanent_reject_reason "
        "FROM agents_meta WHERE id = %s",
        (child_id,),
    ).fetchone()
    assert row == (0, None, None)
