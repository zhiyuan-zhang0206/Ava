"""Shared cancel-race fixture owned by the LLM cancellation package.

Consumer-local conftests and existing file scopes bind this opt-in fixture to
its LLM, exec and compact consumers. Tests execute their real node logic while
controlling the durable-interrupt subscription event.
"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import pytest
from psycopg_pool import AsyncConnectionPool

from agent.graph.exec import node
from agent.graph.interrupt import InterruptEvent
from agent.graph.llm import _cancel
from agent.hooks import compact
from base.agents.incarnation.native_work_models import NativeWorkTarget
from base.native_process.runtime_incarnation import RuntimeIncarnation
from base.native_process.turn_identity import HostedTurnResources


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
        resources: HostedTurnResources | None,
    ) -> AsyncGenerator[InterruptEvent]:
        yield event

    monkeypatch.setattr(_cancel, "subscribe_interrupt", fake_subscribe)
    monkeypatch.setattr(node, "subscribe_interrupt", fake_subscribe)
    monkeypatch.setattr(compact, "subscribe_interrupt", fake_subscribe)
    return event
