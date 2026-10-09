"""Shared cancel-race fixture owned by the LLM cancellation package.

The root plugin registration makes this opt-in fixture available to its
LLM, exec and compact consumers. Tests still execute their real node logic while
controlling the durable-interrupt subscription event.
"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import pytest
from psycopg_pool import AsyncConnectionPool

from agent.graph.interrupt import InterruptEvent
from base.agents.incarnation.native_work_models import NativeWorkTarget
from base.native_process.runtime_incarnation import RuntimeIncarnation


@pytest.fixture
def fake_cancel_event(monkeypatch: pytest.MonkeyPatch) -> InterruptEvent:
    event = InterruptEvent()

    @asynccontextmanager
    async def fake_subscribe(
        _pool: AsyncConnectionPool | None,
        _agent_id: int,
        *,
        incarnation: RuntimeIncarnation | None,
        work: NativeWorkTarget | None,
    ) -> AsyncGenerator[InterruptEvent]:
        yield event

    monkeypatch.setattr("agent.graph.llm._cancel.subscribe_interrupt", fake_subscribe)
    monkeypatch.setattr("agent.graph.exec.node.subscribe_interrupt", fake_subscribe)
    monkeypatch.setattr("agent.hooks.compact.subscribe_interrupt", fake_subscribe)
    return event
