"""ava_builtins.plugins.ava_silent_idle test — the before_llm hook that injects a Continue
nudge when the previous turn was a silent idle (a reasoning-only AIMessage tail:
no text, no tool_call).

The hook is a graph-edge node: it reads the message tail off `state` and returns
a delta dict (or None). These tests import the plugin's agent-runtime face, build
an AgentState, and call the hook directly — mirroring test_ava_sdk_reminder_plugin.py.

Covered:
- injects a system_note tagged SILENT_IDLE_CONTINUE when the tail is a
  reasoning-only AIMessage
- no-op when the tail has text / a tool_call / is a HumanMessage / is empty
- defers (no inject) when auto-compact would replace messages the same turn
  (two before_llm hooks writing `messages` in one pass would collide)
"""

import sys
from collections.abc import Iterator
from types import ModuleType
from typing import Any
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.runtime import Runtime

from agent.messages import NoteTag
from agent.state import build_agent_state
from base.agents.context import AvaContext
from base.db import Database
from base.events.live.bus import EventBus
from base.host.env.agent_slices import AgentSlices, ModelOverrides
from base.lm.catalog import ModelCatalog
from base.packages.plugins.extensions import EMPTY


@pytest.fixture
def _loaded() -> Iterator[ModuleType]:
    """Import the ava_silent_idle agent-runtime face fresh; teardown unloads the module."""
    for name in list(sys.modules):
        if name.startswith("ava_builtins.plugins.ava_silent_idle"):
            del sys.modules[name]

    from ava_builtins.plugins.ava_silent_idle import agent_runtime as _plugin

    yield _plugin

    for name in list(sys.modules):
        if name.startswith("ava_builtins.plugins.ava_silent_idle"):
            del sys.modules[name]


def _state(messages: list[AnyMessage]):
    return build_agent_state(EMPTY)(messages=messages)


def _runtime(catalog: ModelCatalog) -> Runtime[AvaContext]:
    ctx = AvaContext(
        ops_pool=MagicMock(),
        llm=MagicMock(),
        event_publisher=MagicMock(),
        agent=AgentSlices.resolve(),
        catalog=catalog,
        db=Database.from_settings(),
        bus=EventBus.from_settings(),
    )
    return Runtime(context=ctx)


def _config() -> RunnableConfig:
    return {"configurable": {"thread_id": "1"}}


def _reasoning_only_ai() -> AIMessage:
    """Reasoning-only AIMessage: a thinking block, no text, no tool_calls —
    exactly what the kernel commits on a silent-idle continue-loop."""
    return AIMessage(content=[{"type": "thinking", "thinking": "hmm", "signature": "s"}])


async def test_injects_nudge_when_tail_is_reasoning_only(
    _loaded: ModuleType, model_catalog: ModelCatalog
):
    state = _state([HumanMessage(content="hi"), _reasoning_only_ai()])
    result = await _loaded.silent_idle_continue_before_llm(
        state, _runtime(model_catalog), _config()
    )  # pyright: ignore[reportUnknownMemberType]
    assert result is not None
    msgs = result["messages"]
    assert len(msgs) == 1  # pyright: ignore[reportUnknownArgumentType]
    note = msgs[0]
    assert note.additional_kwargs["ava_msg_type"] == "system_note"  # pyright: ignore[reportUnknownMemberType]
    assert note.additional_kwargs["ava_note_tag"] == NoteTag.SILENT_IDLE_CONTINUE  # pyright: ignore[reportUnknownMemberType]


async def test_noop_when_tail_has_text(_loaded: ModuleType, model_catalog: ModelCatalog):
    state = _state([HumanMessage(content="hi"), AIMessage(content="done")])
    assert (
        await _loaded.silent_idle_continue_before_llm(state, _runtime(model_catalog), _config())
        is None
    )  # pyright: ignore[reportUnknownMemberType]


async def test_noop_when_tail_has_tool_call(_loaded: ModuleType, model_catalog: ModelCatalog):
    ai = AIMessage(
        content="", tool_calls=[{"name": "execute_code", "args": {"code": "1"}, "id": "c1"}]
    )
    state = _state([HumanMessage(content="hi"), ai])
    assert (
        await _loaded.silent_idle_continue_before_llm(state, _runtime(model_catalog), _config())
        is None
    )  # pyright: ignore[reportUnknownMemberType]


async def test_noop_when_tail_is_human(_loaded: ModuleType, model_catalog: ModelCatalog):
    # A reasoning-only AIMessage that is NOT the tail must not trigger.
    state = _state([_reasoning_only_ai(), HumanMessage(content="hi")])
    assert (
        await _loaded.silent_idle_continue_before_llm(state, _runtime(model_catalog), _config())
        is None
    )  # pyright: ignore[reportUnknownMemberType]


async def test_noop_when_empty(_loaded: ModuleType, model_catalog: ModelCatalog):
    assert (
        await _loaded.silent_idle_continue_before_llm(
            _state([]), _runtime(model_catalog), _config()
        )
        is None
    )  # pyright: ignore[reportUnknownMemberType]


async def test_defers_when_auto_compact_would_fire(
    _loaded: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    loguru_records: list[dict[str, Any]],
    model_catalog: ModelCatalog,
):
    """When auto-compact would replace messages this turn, the nudge defers
    (returns None) so it does not collide with compaction's `messages` write —
    and the defer log names the registered `silent_idle` event (the raw
    'silent-idle' label is not an event and would raise in the emitter)."""
    # Pin the force-compact ceiling to 1 token (regardless of model) so any
    # non-empty history triggers it; occupancy here is the chars/4 fallback.
    from base.lm.context_budget import ContextBudget

    budget = ContextBudget(
        max_context_tokens=1_000_000, soft_compact_tokens=600_000, hard_compact_tokens=1
    )

    def pinned_budget(
        _model: str, _overrides: ModelOverrides, *, catalog: ModelCatalog
    ) -> ContextBudget:
        assert catalog is model_catalog
        return budget

    monkeypatch.setattr("agent.hooks.compact.resolve_context_budget", pinned_budget)
    state = _state([HumanMessage(content="a long history " * 20), _reasoning_only_ai()])
    assert (
        await _loaded.silent_idle_continue_before_llm(state, _runtime(model_catalog), _config())
        is None
    )  # pyright: ignore[reportUnknownMemberType]
    assert any(
        record["extra"].get("label") == "silent-idle"
        and record["extra"].get("event") == "silent_idle"
        for record in loguru_records
    )
