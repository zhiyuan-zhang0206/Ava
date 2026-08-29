"""The passive-recall hook's trigger gate (`plugins/ava_memory/plugin.py`).

The hook fires on any inbound that is not a machine-originated wake-up: user
chat, a peer agent's message (`agent:`), a scheduled turn (`schedule:`), and
system notices all pass; `watcher:` and `shell:` completions are skipped. These
tests drive the real hook with the recall pass stubbed, so they cover the gate
(and the no-inbound no-op) — not the search/filter pipeline, which
`tests/agent/test_memory_recall.py` owns.
"""

import sys
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from langchain_core.messages import AIMessage, AnyMessage
from langchain_core.runnables import RunnableConfig
from langgraph.runtime import Runtime

from agent.graph._context import AvaContext
from agent.graph._memory_recall import PassiveRecall
from agent.messages import NoteTag, inbound_message, system_note_message
from agent.state import build_agent_state, clear_plugin_registrations
from ava import _gateway_client


@pytest.fixture
def _loaded() -> Any:
    """Load ava_memory through the real plugin-registration path, so the hook
    instance under test is the registered one (same fixture shape as
    test_ava_memory_notes.py)."""
    from shared.plugin_config_registry import bind_from_disk
    from shared.plugin_context import PluginContext

    clear_plugin_registrations()
    for name in list(sys.modules):
        if name.startswith("ava_builtins.plugins.ava_memory"):
            del sys.modules[name]

    with PluginContext("ava_memory"):
        from ava_builtins.plugins.ava_memory import plugin as _plugin

    bind_from_disk()
    yield _plugin

    clear_plugin_registrations()
    for name in list(sys.modules):
        if name.startswith("ava_builtins.plugins.ava_memory"):
            del sys.modules[name]


@pytest.fixture
def _hook_env(_loaded: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Feature on, auto-compact off, recall stubbed to a successful pass."""
    from shared.config import settings

    monkeypatch.setattr(settings.agent, "passive_memory_recall_enabled", True)
    monkeypatch.setattr(_loaded, "auto_compact_will_fire", lambda _state: False)  # pyright: ignore[reportUnknownArgumentType]
    recall = AsyncMock(
        return_value=PassiveRecall(
            note=system_note_message(content="recalled", tag=NoteTag.MEMORY),
            paths={"a.md"},
        )
    )
    monkeypatch.setattr(_loaded, "passive_memory_recall", recall)
    return _loaded, recall


def _state(messages: list[AnyMessage], **fields: Any):
    return build_agent_state()(messages=messages, **fields)


def _runtime() -> Runtime[AvaContext]:
    ctx = AvaContext(ops_pool=MagicMock(), llm=MagicMock(), event_publisher=MagicMock())
    return Runtime(context=ctx)


def _config() -> RunnableConfig:
    return RunnableConfig()


def _inbound(source: str) -> AnyMessage:
    return inbound_message(content="hello", source=source, inbound_id=1)


@pytest.mark.parametrize(
    "source",
    ["user", "agent:402", "schedule:7", "system"],
    ids=["user", "agent", "schedule", "system"],
)
async def test_fires_on_real_inbound_sources(_hook_env: Any, source: str) -> None:
    """User chat, peer-agent messages, scheduled turns, and system notices all
    carry conversation the recall should search over."""
    _loaded, recall = _hook_env
    hook = _loaded.passive_memory_recall_before_llm
    state = _state([AIMessage(content="prev", id="a0"), _inbound(source)])

    result = await hook(state, _runtime(), _config())

    recall.assert_awaited_once()
    assert result is not None
    assert "a.md" in result["memory"].injected_paths


@pytest.mark.parametrize(
    "source",
    ["watcher:3", "shell:9"],
    ids=["watcher", "shell"],
)
async def test_skips_machine_wakeups(_hook_env: Any, source: str) -> None:
    """Watcher and shell completions are machine-originated wake-ups whose
    payload is the notice itself — recalling durable notes over them is noise."""
    _loaded, recall = _hook_env
    hook = _loaded.passive_memory_recall_before_llm
    state = _state([AIMessage(content="prev", id="a0"), _inbound(source)])

    result = await hook(state, _runtime(), _config())

    recall.assert_not_awaited()
    assert result is None


async def test_fires_when_a_real_inbound_sits_among_wakeups(_hook_env: Any) -> None:
    """A mixed tail with one real inbound still fires — the gate rejects the
    turn only when *every* inbound is a wake-up."""
    _loaded, recall = _hook_env
    hook = _loaded.passive_memory_recall_before_llm
    state = _state(
        [
            AIMessage(content="prev", id="a0"),
            _inbound("watcher:3"),
            _inbound("agent:402"),
        ]
    )

    result = await hook(state, _runtime(), _config())

    recall.assert_awaited_once()
    assert result is not None


async def test_gateway_error_leaves_the_turn_running(
    _loaded: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole hook path — not just the recall function — survives a memory
    search that errors out.

    This is the shape that killed agent 405 on 2026-08-07: the hook runs inside
    `before_llm`, so an exception from the search unwinds the graph and ends the
    agent process. The real `passive_memory_recall` runs here (the other tests
    stub it), with only the gateway call replaced, so the assertion is that the
    turn continues with no recall to add.
    """
    from shared.config import settings

    monkeypatch.setattr(settings.agent, "passive_memory_recall_enabled", True)
    monkeypatch.setattr(_loaded, "auto_compact_will_fire", lambda _state: False)  # pyright: ignore[reportUnknownArgumentType]

    request = httpx.Request("POST", "http://gateway.test/api/memory/search")

    def _boom(_query: str, _k: int):
        raise httpx.HTTPStatusError(
            "Server error '500'", request=request, response=httpx.Response(500, request=request)
        )

    monkeypatch.setattr(_gateway_client, "memory_search", _boom)

    hook = _loaded.passive_memory_recall_before_llm
    state = _state([AIMessage(content="prev", id="a0"), _inbound("user")])

    assert await hook(state, _runtime(), _config()) is None


async def test_recall_deadline_exceeded_skips_recall_this_turn(
    _loaded: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A recall pass slower than `memory_recall_deadline_seconds` degrades to
    no recall instead of stalling the turn's first LLM call.

    The 2026-08-29 first-before_llm 13s shape: a fleet wake queued searches
    behind the gateway's search endpoint semaphore, the recall pass waited the
    full queue time, and the agent's first LLM call waited with it. The
    deadline releases the turn; the next inbound turn retries recall.
    """
    import asyncio

    from shared.config import settings

    monkeypatch.setattr(settings.agent, "passive_memory_recall_enabled", True)
    monkeypatch.setattr(settings.agent, "memory_recall_deadline_seconds", 0.05)
    monkeypatch.setattr(_loaded, "auto_compact_will_fire", lambda _state: False)  # pyright: ignore[reportUnknownArgumentType]

    async def _slow_recall(_messages: Any, **_kwargs: Any) -> PassiveRecall:
        await asyncio.sleep(0.5)
        return PassiveRecall(
            note=system_note_message(content="late", tag=NoteTag.MEMORY),
            paths={"late.md"},
        )

    monkeypatch.setattr(_loaded, "passive_memory_recall", _slow_recall)

    hook = _loaded.passive_memory_recall_before_llm
    state = _state([AIMessage(content="prev", id="a0"), _inbound("user")])

    assert await hook(state, _runtime(), _config()) is None


async def test_no_inbound_tail_is_a_noop(_hook_env: Any) -> None:
    """A silent-idle continue (bare AIMessage tail) carries no inbound at all —
    skipped, and mutually exclusive with hooks that claim that shape."""
    _loaded, recall = _hook_env
    hook = _loaded.passive_memory_recall_before_llm
    state = _state([AIMessage(content="idle", id="a0")])

    result = await hook(state, _runtime(), _config())

    recall.assert_not_awaited()
    assert result is None
