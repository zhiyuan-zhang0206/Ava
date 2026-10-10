"""`llm_node` turn_end finally-path unit tests — ok=False MUST carry a traceback.

161 incident: turn_end ok=False but neither events nor stderr had exception info —
`logger.info` in the finally block emitted but didn't capture exc_info, so from the
framework's perspective "why it failed" was completely invisible. Fix:
`logger.opt(exception=True).warning(...)` on the ok=False path, together with
`base.log._postgres_sink` automatically injecting traceback / exception_type /
exception_value into events.payload.

Tests focus on the two branches of the finally block (ok=True / ok=False), without
re-testing _llm_node_impl internals — use monkeypatch to make _llm_node_impl
immediately raise / return a mock.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest
from langchain_core.runnables import RunnableConfig

from agent.graph.llm._retry import Attempt
from agent.graph.llm.node import llm_attempt, llm_node
from agent.graph.llm_errors import LlmLedger
from agent.tests._fakes import make_fake_ops_pool
from base.agents.context import AvaContext
from base.clock import Clock
from base.config import settings
from base.host.env.agent_slices import AgentSlices
from base.lm.catalog import ModelCatalog
from base.native_process.turn_identity import HostedTurnResources
from tests.fixtures.pin_agent import hosted_resources as hosted_resources


def _runtime_with_redis(hosted_resources: HostedTurnResources, catalog: ModelCatalog):
    """MagicMock runtime + functional ops_pool (node_lifecycle reads chat
    anchors on enter; subscribe_interrupt's watcher reads pending interrupts)."""
    runtime = MagicMock()
    runtime.execution_info = None
    runtime.context = AvaContext(
        hosted_resources=hosted_resources,
        ops_pool=make_fake_ops_pool(),
        event_publisher=MagicMock(),
        agent=AgentSlices.resolve(
            default_reader=lambda domain, field: getattr(getattr(settings, domain), field)
        ),
        catalog=catalog,
        clock_factory=Clock.from_settings,
    )
    return runtime


def _config_with_thread() -> RunnableConfig:
    """agent_id is read from config["configurable"]["thread_id"], must be indexable to str."""
    return {"configurable": {"thread_id": "1"}}


async def test_turn_end_ok_false_record_carries_exception(
    hosted_resources: HostedTurnResources,
    monkeypatch: pytest.MonkeyPatch,
    loguru_records,
    model_catalog: ModelCatalog,
) -> None:
    """`_llm_node_impl` raises → finally uses logger.opt(exception=True) so that
    record["exception"] is not None, allowing downstream _postgres_sink to inject the traceback."""

    async def _boom(*_a, **_k):
        raise RuntimeError("simulated LLM timeout 39s")

    monkeypatch.setattr("agent.graph.llm.node._llm_node_impl", _boom)  # pyright: ignore[reportUnknownArgumentType]

    with pytest.raises(RuntimeError, match="simulated LLM timeout"):
        await llm_attempt(
            state=MagicMock(messages=[]),
            runtime=_runtime_with_redis(hosted_resources=hosted_resources, catalog=model_catalog),
            config=_config_with_thread(),
            attempt=Attempt(1, time.time()),
            ledger=LlmLedger(),
        )

    turn_end_records = [r for r in loguru_records if r["extra"].get("event") == "turn_end"]  # pyright: ignore[reportUnknownMemberType]
    assert len(turn_end_records) == 1  # pyright: ignore[reportUnknownArgumentType]
    rec = turn_end_records[0]
    assert rec["extra"]["ok"] is False
    assert (
        rec["level"].name == "WARNING"  # pyright: ignore[reportUnknownMemberType]
    )  # ok=False goes through warning, distinct from ok=True info
    assert rec["exception"] is not None, (
        "logger.opt(exception=True) must attach an exception object — _postgres_sink relies on it to inject traceback"
    )
    assert rec["exception"].type is RuntimeError  # pyright: ignore[reportUnknownMemberType]
    assert "simulated LLM timeout" in str(rec["exception"].value)  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]


async def test_turn_end_ok_true_record_no_exception(
    hosted_resources: HostedTurnResources,
    monkeypatch: pytest.MonkeyPatch,
    loguru_records,
    model_catalog: ModelCatalog,
) -> None:
    """Normal path ok=True: uses logger.info without exc_info, record["exception"] is None,
    payload must not bloat with traceback/exception_* fields."""
    sentinel = MagicMock()

    async def _ok(*_a, **_k):
        return sentinel

    monkeypatch.setattr("agent.graph.llm.node._llm_node_impl", _ok)  # pyright: ignore[reportUnknownArgumentType]

    result = await llm_node(
        state=MagicMock(),
        runtime=_runtime_with_redis(hosted_resources=hosted_resources, catalog=model_catalog),
        config=_config_with_thread(),
        ledger=LlmLedger(),
    )
    assert result is sentinel

    turn_end_records = [r for r in loguru_records if r["extra"].get("event") == "turn_end"]  # pyright: ignore[reportUnknownMemberType]
    assert len(turn_end_records) == 1  # pyright: ignore[reportUnknownArgumentType]
    rec = turn_end_records[0]
    assert rec["extra"]["ok"] is True
    assert rec["level"].name == "INFO"  # pyright: ignore[reportUnknownMemberType]
    assert rec["exception"] is None
