from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import pytest
from psycopg_pool import AsyncConnectionPool

from agent.graph.interrupt import InterruptEvent
from agent.graph.llm import _cancel
from base.agents.incarnation.native_work_models import NativeWorkTarget
from base.native_process.runtime_incarnation import RuntimeIncarnation
from base.native_process.turn_identity import HostedTurnResources


def install_cancel_event(monkeypatch: pytest.MonkeyPatch, event: InterruptEvent) -> None:

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


@pytest.fixture
def fake_cancel_event(monkeypatch: pytest.MonkeyPatch) -> InterruptEvent:
    event = InterruptEvent()
    install_cancel_event(monkeypatch, event)
    return event
