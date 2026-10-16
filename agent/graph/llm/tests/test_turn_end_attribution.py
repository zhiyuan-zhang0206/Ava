from __future__ import annotations

import time
from collections.abc import AsyncIterator
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessageChunk, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.runtime import Runtime

from agent.graph.interrupt import InterruptEvent
from agent.graph.llm._retry import Attempt
from agent.graph.llm.node import llm_attempt
from agent.graph.llm_errors import LlmLedger
from agent.hooks.tests.fakes import make_fake_ops_pool
from agent.state import AgentState
from ava.tests.fixtures.pin_agent import hosted_resources as hosted_resources
from base.agents.context import AvaContext
from base.clock import Clock
from base.config.tests.configuration import defaults
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.host.env.agent_slices import AgentSlices
from base.lm.catalog import ModelCatalog
from base.log.tests.log_records import LogRecords
from base.native_process.turn_identity import HostedTurnResources

_CONFIG: RunnableConfig = {"configurable": {"thread_id": "1"}}


def _make_runtime(
    hosted_resources: HostedTurnResources,
    llm: MagicMock,
    *,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
) -> Runtime[AvaContext]:
    llm.bind_tools.return_value = llm
    ctx = AvaContext(
        hosted_resources=hosted_resources,
        ops_pool=make_fake_ops_pool(),
        llm=llm,
        event_publisher=MagicMock(),
        agent=AgentSlices.resolve(default_reader=defaults()),
        db=Database.from_settings(gate=database_gate),
        bus=EventBus.from_settings(),
        catalog=model_catalog,
        clock_factory=Clock.from_settings,
    )
    return Runtime(context=ctx)


async def test_turn_end_records_carry_the_owning_agent(
    hosted_resources: HostedTurnResources,
    fake_cancel_event: InterruptEvent,
    loguru_records: LogRecords,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
) -> None:
    async def _complete() -> AsyncIterator[AIMessageChunk]:
        yield AIMessageChunk(
            content="ok",
            response_metadata={"model_provider": "anthropic", "stop_reason": "end_turn"},
            usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        )

    llm = MagicMock()
    llm.astream.return_value = _complete()
    state = AgentState(messages=[HumanMessage(content="hi")], halted=False)

    result = await llm_attempt(
        state,
        _make_runtime(
            hosted_resources, llm, model_catalog=model_catalog, database_gate=database_gate
        ),
        _CONFIG,
        Attempt(1, time.time()),
        LlmLedger(),
    )
    assert result is not None

    records = loguru_records.events("turn_end")
    assert len(records) == 1
    assert records[0]["extra"]["ok"] is True
    assert records[0]["extra"]["agent_id"] == 1


async def test_turn_end_failure_records_carry_the_owning_agent(
    hosted_resources: HostedTurnResources,
    fake_cancel_event: InterruptEvent,
    loguru_records: LogRecords,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
) -> None:
    llm = MagicMock()
    llm.astream.side_effect = RuntimeError("simulated provider failure")
    state = AgentState(messages=[HumanMessage(content="hi")], halted=False)

    with pytest.raises(RuntimeError, match="simulated provider failure"):
        await llm_attempt(
            state,
            _make_runtime(
                hosted_resources, llm, model_catalog=model_catalog, database_gate=database_gate
            ),
            _CONFIG,
            Attempt(1, time.time()),
            LlmLedger(),
        )

    records = loguru_records.events("turn_end")
    assert len(records) == 1
    assert records[0]["extra"]["ok"] is False
    assert records[0]["extra"]["agent_id"] == 1
