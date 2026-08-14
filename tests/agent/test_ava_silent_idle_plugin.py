"""ava_builtins.plugins.ava_silent_idle test — the before_llm hook that injects a Continue
nudge when the previous turn was a silent idle (a reasoning-only AIMessage tail:
no text, no tool_call).

The hook is a graph-edge node: it reads the message tail off `state` and returns
a delta dict (or None). These tests load the plugin via the real registration
path, build an AgentState, and call the hook directly — mirroring
test_ava_sdk_reminder_plugin.py.

Covered:
- injects a system_note tagged SILENT_IDLE_CONTINUE when the tail is a
  reasoning-only AIMessage
- no-op when the tail has text / a tool_call / is a HumanMessage / is empty
- defers (no inject) when auto-compact would replace messages the same turn
  (two before_llm hooks writing `messages` in one pass would collide)
"""

import sys
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.runtime import Runtime

from agent.graph._context import AvaContext
from agent.messages import NoteTag
from agent.state import build_agent_state, clear_plugin_registrations


@pytest.fixture
def _loaded():
    """Load plugins.ava_silent_idle via the real plugin-registration path;
    teardown clears registrations + unloads the module so its before_llm hook
    does not leak into other tests."""
    from shared.plugin_context import PluginContext

    clear_plugin_registrations()
    for name in list(sys.modules):
        if name.startswith("ava_builtins.plugins.ava_silent_idle"):
            del sys.modules[name]

    with PluginContext("ava_silent_idle"):
        from ava_builtins.plugins.ava_silent_idle import plugin as _plugin

    yield _plugin

    clear_plugin_registrations()
    for name in list(sys.modules):
        if name.startswith("ava_builtins.plugins.ava_silent_idle"):
            del sys.modules[name]


def _state(messages: list[AnyMessage]):
    return build_agent_state()(messages=messages)


def _runtime() -> Runtime[AvaContext]:
    ctx = AvaContext(ops_pool=MagicMock(), llm=MagicMock(), event_publisher=MagicMock())
    return Runtime(context=ctx)


def _config() -> RunnableConfig:
    return {"configurable": {"thread_id": "1"}}


def _reasoning_only_ai() -> AIMessage:
    """Reasoning-only AIMessage: a thinking block, no text, no tool_calls —
    exactly what the kernel commits on a silent-idle continue-loop."""
    return AIMessage(content=[{"type": "thinking", "thinking": "hmm", "signature": "s"}])


async def test_injects_nudge_when_tail_is_reasoning_only(_loaded):
    state = _state([HumanMessage(content="hi"), _reasoning_only_ai()])
    result = await _loaded.silent_idle_continue_before_llm(state, _runtime(), _config())  # pyright: ignore[reportUnknownMemberType]
    assert result is not None
    msgs = result["messages"]
    assert len(msgs) == 1  # pyright: ignore[reportUnknownArgumentType]
    note = msgs[0]
    assert note.additional_kwargs["ava_msg_type"] == "system_note"  # pyright: ignore[reportUnknownMemberType]
    assert note.additional_kwargs["ava_note_tag"] == NoteTag.SILENT_IDLE_CONTINUE  # pyright: ignore[reportUnknownMemberType]


async def test_noop_when_tail_has_text(_loaded):
    state = _state([HumanMessage(content="hi"), AIMessage(content="done")])
    assert await _loaded.silent_idle_continue_before_llm(state, _runtime(), _config()) is None  # pyright: ignore[reportUnknownMemberType]


async def test_noop_when_tail_has_tool_call(_loaded):
    ai = AIMessage(
        content="", tool_calls=[{"name": "execute_code", "args": {"code": "1"}, "id": "c1"}]
    )
    state = _state([HumanMessage(content="hi"), ai])
    assert await _loaded.silent_idle_continue_before_llm(state, _runtime(), _config()) is None  # pyright: ignore[reportUnknownMemberType]


async def test_noop_when_tail_is_human(_loaded):
    # A reasoning-only AIMessage that is NOT the tail must not trigger.
    state = _state([_reasoning_only_ai(), HumanMessage(content="hi")])
    assert await _loaded.silent_idle_continue_before_llm(state, _runtime(), _config()) is None  # pyright: ignore[reportUnknownMemberType]


async def test_noop_when_empty(_loaded):
    assert await _loaded.silent_idle_continue_before_llm(_state([]), _runtime(), _config()) is None  # pyright: ignore[reportUnknownMemberType]


async def test_defers_when_auto_compact_would_fire(_loaded, monkeypatch: pytest.MonkeyPatch):
    """When auto-compact would replace messages this turn, the nudge defers
    (returns None) so it does not collide with compaction's `messages` write."""
    # Pin the force-compact ceiling to 1 token (regardless of model) so any
    # non-empty history triggers it; occupancy here is the chars/4 fallback.
    from shared.lm.context_budget import ContextBudget

    budget = ContextBudget(
        max_context_tokens=1_000_000, soft_compact_tokens=600_000, hard_compact_tokens=1
    )
    monkeypatch.setattr("agent.hooks.compact.resolve_context_budget", lambda _model: budget)  # pyright: ignore[reportUnknownArgumentType]
    state = _state([HumanMessage(content="a long history " * 20), _reasoning_only_ai()])
    assert await _loaded.silent_idle_continue_before_llm(state, _runtime(), _config()) is None  # pyright: ignore[reportUnknownMemberType]
