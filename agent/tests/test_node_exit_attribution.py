from __future__ import annotations

from collections.abc import Iterator
from contextlib import AbstractAsyncContextManager
from typing import Any
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import SystemMessage

from agent.graph import node_log
from agent.graph.node_log import node_lifecycle
from base.agents.history.timeline_inputs import TimelineReadInputs
from base.agents.observation.turn_progress import TurnProgress
from base.clock import Clock
from base.config import settings
from base.log.tests.log_records import LogRecords

_TIMELINE_INPUTS = TimelineReadInputs(
    Clock.from_settings, lambda: settings.general.message_timestamps
)


def _exit_records(records: LogRecords) -> list[dict[str, Any]]:
    return [r for r in records if r["extra"].get("event") == "node_exit"]


def _wrap(node: str, msg_count: int) -> AbstractAsyncContextManager[None]:
    return node_lifecycle(
        node,
        messages=[SystemMessage(content="x")] * msg_count,
        event_publisher=MagicMock(),
        agent_id=1,
        turn_progress=TurnProgress(),
        read_stall_seconds=lambda: settings.agent.node_stall_dump_seconds,
        timeline_inputs=_TIMELINE_INPUTS,
        limit_reader=lambda: settings.display.timeline_default_limit,
    )


@pytest.fixture(autouse=True)
def _isolate_node_exit_aggregate() -> Iterator[None]:
    aggregate = getattr(node_log, "_NODE_EXIT_AGGREGATE", {})
    aggregate.clear()
    yield
    aggregate.clear()


async def test_node_exit_records_carry_the_owning_agent(loguru_records: LogRecords) -> None:
    async def _body() -> None:
        async with _wrap("exec", 1):
            raise RuntimeError("simulated exec crash")

    async with _wrap("llm", 1):
        pass

    with pytest.raises(RuntimeError, match="simulated exec crash"):
        await _body()

    node_log.flush_node_exit_aggregate(1)

    exits = _exit_records(loguru_records)
    assert len(exits) == 2
    assert [rec["extra"]["agent_id"] for rec in exits] == [1, 1]
