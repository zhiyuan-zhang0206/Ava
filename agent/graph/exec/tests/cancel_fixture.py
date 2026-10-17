"""Cancel-race fixture for the exec package, and the runtime helpers its cancel tests share.

Tests control the durable-interrupt subscription event; `install_cancel_event` is also used by
the graph-level fixture that combines the llm, exec and compact patches."""

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage
from langgraph.runtime import Runtime
from psycopg_pool import AsyncConnectionPool

from agent.graph.exec import node
from agent.graph.interrupt import InterruptEvent
from agent.hooks.tests.fakes import make_fake_ops_pool
from base.agents.context import MeivaContext
from base.agents.incarnation.native_work_models import NativeWorkTarget
from base.clock import Clock
from base.config.tests.configuration import defaults
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.events.live.projection import EVENT_ADAPTER, Cancelled
from base.host.env.agent_slices import AgentSlices
from base.native_process.runtime_incarnation import RuntimeIncarnation
from base.native_process.turn_identity import HostedTurnResources
from base.packages.plugin_providers import build_model_catalog
from meiva.tests.fixtures.pin_agent import hosted_resources as hosted_resources


def install_cancel_event(monkeypatch: pytest.MonkeyPatch, event: InterruptEvent) -> None:
    """Make this package's durable-interrupt subscription yield `event`."""

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

    monkeypatch.setattr(node, "subscribe_interrupt", fake_subscribe)


@pytest.fixture
def fake_cancel_event(monkeypatch: pytest.MonkeyPatch) -> InterruptEvent:
    event = InterruptEvent()
    install_cancel_event(monkeypatch, event)
    return event


def has_cancelled_event(pub: MagicMock, agent_id: int) -> bool:
    """assert helper: whether pub.emit calls contain Cancelled(agent_id=...)."""
    for call in pub.emit.call_args_list:
        (payload,) = call.args
        try:
            ev = EVENT_ADAPTER.validate_json(payload)
        except Exception:  # noqa: S112 — skip payloads outside the events union
            continue
        if isinstance(ev, Cancelled) and ev.agent_id == agent_id:
            return True
    return False


def make_runtime(
    hosted_resources: HostedTurnResources,
    *,
    llm=None,
    ops_pool=None,
    event_publisher=None,
    database_gate: ProcessDbGate,
) -> Runtime[MeivaContext]:
    """Test helper: assemble an MeivaContext and wrap it in Runtime."""
    if llm is None:
        llm = MagicMock()
    if isinstance(llm, MagicMock):
        llm.bind_tools.return_value = llm
    if ops_pool is None:
        ops_pool = make_fake_ops_pool()
    ctx = MeivaContext(
        hosted_resources=hosted_resources,
        ops_pool=ops_pool,  # pyright: ignore[reportUnknownArgumentType]
        llm=llm,  # pyright: ignore[reportUnknownArgumentType]
        event_publisher=event_publisher if event_publisher is not None else MagicMock(),  # pyright: ignore[reportUnknownArgumentType]
        agent=AgentSlices.resolve(default_reader=defaults()),
        db=Database.from_settings(gate=database_gate),
        bus=EventBus.from_settings(),
        catalog=build_model_catalog(),
        clock_factory=Clock.from_settings,
    )
    return Runtime(context=ctx)


def ai_with_code(code: str) -> AIMessage:
    """Single tool wire format: AIMessage with execute_code tool_call."""
    return AIMessage(
        content="",
        tool_calls=[{"name": "execute_code", "args": {"code": code}, "id": "call_test"}],
    )


async def set_after(ev: asyncio.Event, delay: float) -> None:
    """Helper: set event after `delay` seconds — used as a cancel_event trigger."""
    await asyncio.sleep(delay)
    ev.set()


def committed_messages(update: dict) -> list:
    """The cancel discard path must not commit any message — extract the messages
    from update (missing key or empty list counts as "not committed")."""
    return update.get("messages", [])  # pyright: ignore[reportUnknownMemberType]
