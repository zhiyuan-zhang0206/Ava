"""agent/hooks registry + runner behavior guard.

Test Hook subclass registration path, runner merge semantics, Command(goto=) routing (including hook override), fail-fast exception propagation. hooks changed from functions to `Hook` subclass instances: registration accepts instance, runner calls with `hook(state, runtime, config)` (instance is callable, call site unchanged). HOOKS is module-level state, conftest fixture snapshots/restores before/after each test to avoid cross-test contamination.
"""

from typing import cast
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.runtime import Runtime
from langgraph.types import Command

from agent.graph._context import AvaContext
from agent.hooks import (
    HOOKS,
    Hook,
    HookName,
    make_hook_runner,
    register_before_exec,
    register_before_llm,
)
from agent.state import AgentState
from shared import plugin_activation, plugin_contributions
from shared.plugin_context import PluginContext
from tests.agent._fakes import make_fake_ops_pool


@pytest.fixture(autouse=True)
def _isolate_hooks():
    """Before each test clear HOOKS, restore after — avoid cross-test contamination + not lose production
    registration (if import chain has register side effects)."""
    saved = {k: list(v) for k, v in HOOKS.items()}
    for v in HOOKS.values():
        v.clear()
    yield
    for k, v in saved.items():
        # cast is because dict.items() degrades key to str
        HOOKS[cast(HookName, k)][:] = v


# ── Test hook subclasses ──────────────────────────────────────────────────
# Small `Hook` subclasses standing in for plugin hooks. `_ReturnHook` and
# `_RecordHook` carry their behavior as instance state (the dict to return, the
# tag + shared list to append to) — the "instances carry config/state" property
# the class design buys.


class _ReturnHook(Hook):
    """Returns a fixed update dict (or None) every call."""

    def __init__(self, result: dict | None) -> None:
        self._result = result  # pyright: ignore[reportUnknownMemberType]

    async def __call__(self, state, runtime, config, /) -> dict | None:
        return self._result  # pyright: ignore[reportUnknownMemberType]


class _RecordHook(Hook):
    """Appends `tag` to a shared list on each call; contributes no update."""

    def __init__(self, tag: str, calls: list[str]) -> None:
        self._tag = tag
        self._calls = calls

    async def __call__(self, state, runtime, config, /) -> dict | None:
        self._calls.append(self._tag)
        return None


def _empty_state() -> AgentState:
    return AgentState(messages=[], halted=False)


def _empty_runtime() -> Runtime[AvaContext]:
    """Test runtime — AvaContext fields are all mocks, hook doesn't touch them is fine."""
    ctx = AvaContext(
        ops_pool=make_fake_ops_pool(),
        llm=MagicMock(),
        event_publisher=MagicMock(),
    )
    return Runtime(context=ctx)


def _empty_config() -> RunnableConfig:
    """Minimal config with agent_id=1 — hook doesn't read it, but aligns with business node three-arg signature."""
    return {"configurable": {"thread_id": "1"}}


async def test_runner_pass_through_when_no_hooks():
    """No hook registered — runner returns Command(update={}, goto=default_next)."""
    runner = make_hook_runner("before_llm", default_next="llm")
    cmd = await runner(_empty_state(), _empty_runtime(), _empty_config())
    assert isinstance(cmd, Command)
    assert cmd.update == {}
    assert cmd.goto == "llm"


async def test_runner_calls_all_hooks_in_register_order():
    """Multiple hooks registered — run in register order sequentially."""
    calls: list[str] = []
    register_before_llm(_RecordHook("a", calls))
    register_before_llm(_RecordHook("b", calls))

    runner = make_hook_runner("before_llm", default_next="llm")
    await runner(_empty_state(), _empty_runtime(), _empty_config())
    assert calls == ["a", "b"]


async def test_runner_same_key_co_write_raises():
    """Two hooks write same key in one pass — fail-loud RuntimeError, naming both hooks (each `Hook.name`, default class name) + conflict key. Old last-wins silent merge would let one hook's `messages` silently overwrite another's (compaction full replace swallowing sibling's note is concrete scenario), so changed to raise; hooks sharing a node must sequence themselves (sibling defers when the other wants to write)."""

    class _HookFirst(Hook):
        async def __call__(self, state, runtime, config, /) -> dict | None:
            return {"halted": True}

    class _HookSecond(Hook):
        async def __call__(self, state, runtime, config, /) -> dict | None:
            return {"halted": False}  # same key co-write -> raise

    register_before_llm(_HookFirst())
    register_before_llm(_HookSecond())

    with pytest.raises(RuntimeError, match=r"both wrote key 'halted'") as exc:
        await make_hook_runner("before_llm", default_next="llm")(
            _empty_state(), _empty_runtime(), _empty_config()
        )
    # Error must name both hooks (class names) to facilitate locating coordination bug
    assert "_HookFirst" in str(exc.value)
    assert "_HookSecond" in str(exc.value)


async def test_runner_reducer_key_co_write_allowed():
    """Two hooks write same key with reducer (like messages) in one pass — allowed to merge, no raise. add_messages reducer ensures both hooks' messages are appended."""
    register_before_llm(_ReturnHook({"messages": [HumanMessage(content="from hook a")]}))
    register_before_llm(_ReturnHook({"messages": [HumanMessage(content="from hook b")]}))

    cmd = await make_hook_runner("before_llm", default_next="llm")(
        _empty_state(), _empty_runtime(), _empty_config()
    )
    # Both messages should be in the update (reducer merged them)
    assert cmd.update is not None
    msgs = cmd.update.get("messages", [])
    assert len(msgs) == 2
    assert msgs[0].content == "from hook a"
    assert msgs[1].content == "from hook b"


async def test_runner_no_reducer_key_co_write_still_raises():
    """Key without reducer (like halted) still raises when both hooks write simultaneously — silent clobber protection unchanged."""
    register_before_llm(_ReturnHook({"halted": True}))
    register_before_llm(_ReturnHook({"halted": False}))

    with pytest.raises(RuntimeError, match=r"both wrote key 'halted'"):
        await make_hook_runner("before_llm", default_next="llm")(
            _empty_state(), _empty_runtime(), _empty_config()
        )


async def test_runner_co_write_unknown_key_raises():
    """Key not in state schema (like typo) even if appears in both hooks simultaneously will raise — unknown key has no reducer, co-write is a bug."""
    register_before_llm(_ReturnHook({"typo_field": 1}))
    register_before_llm(_ReturnHook({"typo_field": 2}))

    with pytest.raises(RuntimeError, match=r"both wrote key 'typo_field'"):
        await make_hook_runner("before_llm", default_next="llm")(
            _empty_state(), _empty_runtime(), _empty_config()
        )


async def test_runner_disjoint_keys_merge():
    """Two hooks write disjoint keys — normally merge into one update, no raise."""
    register_before_llm(_ReturnHook({"halted": True}))
    register_before_llm(_ReturnHook({"goto": "custom"}))

    cmd = await make_hook_runner("before_llm", default_next="llm")(
        _empty_state(), _empty_runtime(), _empty_config()
    )
    assert cmd.goto == "custom"
    assert cmd.update == {"halted": True}


async def test_runner_skips_none_returns():
    """observation hook returns None — runner skips, not merged into update."""
    register_before_llm(_ReturnHook(None))
    register_before_llm(_ReturnHook({"halted": True}))

    cmd = await make_hook_runner("before_llm", default_next="llm")(
        _empty_state(), _empty_runtime(), _empty_config()
    )
    assert cmd.update == {"halted": True}


async def test_hook_can_override_goto():
    """hook update sets 'goto' → runner uses it instead of default_next.
    'goto' does not enter update fields (popped), used only for routing."""
    register_before_llm(_ReturnHook({"goto": "custom_target", "halted": True}))

    cmd = await make_hook_runner("before_llm", default_next="llm")(
        _empty_state(), _empty_runtime(), _empty_config()
    )
    assert cmd.goto == "custom_target"
    assert cmd.update == {"halted": True}  # goto was popped, only halted remains


async def test_runner_propagates_hook_exceptions():
    """fail-fast — hook raises error lets graph explode, no catch."""

    class _BoomHook(Hook):
        async def __call__(self, state, runtime, config, /) -> dict | None:
            raise RuntimeError("plugin bug")

    register_before_llm(_BoomHook())

    runner = make_hook_runner("before_llm", default_next="llm")
    with pytest.raises(RuntimeError, match="plugin bug"):
        await runner(_empty_state(), _empty_runtime(), _empty_config())


async def test_register_functions_target_correct_lists():
    """Three register_* each put instance into corresponding list, no cross-connection."""
    h1 = _ReturnHook(None)
    h2 = _ReturnHook(None)
    register_before_llm(h1)
    register_before_exec(h2)

    assert HOOKS["before_llm"] == [h1]
    assert HOOKS["before_exec"] == [h2]
    assert HOOKS["after_exec"] == []


async def test_runner_sees_hooks_registered_after_build():
    """make_hook_runner snapshots HOOKS list reference at build time — hooks registered later can also run. This decouples plugin load order from graph build."""
    runner = make_hook_runner("before_llm", default_next="llm")

    calls: list[str] = []
    register_before_llm(_RecordHook("late", calls))

    await runner(_empty_state(), _empty_runtime(), _empty_config())
    assert calls == ["late"]


async def test_hook_can_read_agent_id_from_config():
    """hook reads agent_id via config — verifies LangGraph automatically passes config into hook."""
    from agent.graph._context import agent_id_from_config

    seen: list[int] = []

    class _CaptureTid(Hook):
        async def __call__(self, state, runtime, config, /) -> dict | None:
            seen.append(agent_id_from_config(config))
            return None

    register_before_llm(_CaptureTid())

    runner = make_hook_runner("before_llm", default_next="llm")
    await runner(_empty_state(), _empty_runtime(), {"configurable": {"thread_id": "42"}})
    assert seen == [42]


# ── activation telemetry (issue #40) ────────────────────────────────────────


@pytest.fixture
def activations(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, str, str]]:
    """Capture (plugin, surface, identifier, detail) per recorded activation.

    Keeps the real `record`'s attribution gate — an unattributed firing is
    dropped rather than captured — so these tests read the emitted stream, not
    the call log."""
    recorded: list[tuple[str, str, str, str]] = []

    def spy(plugin: str | None, surface: str, identifier: str, *, detail: str = "") -> None:
        if plugin is not None:
            recorded.append((plugin, surface, identifier, detail))

    monkeypatch.setattr(plugin_activation, "record", spy)
    return recorded


async def test_plugin_hook_state_update_records_activation(
    activations: list[tuple[str, str, str, str]],
):
    """A plugin hook that returned a state update acted on the turn — the record
    names the keys it wrote, which is also how the `state` surface is covered
    (plugin state writes travel through hook returns)."""
    with PluginContext("myplugin"):
        register_before_llm(_ReturnHook({"halted": True}))

    await make_hook_runner("before_llm", default_next="llm")(
        _empty_state(), _empty_runtime(), _empty_config()
    )
    assert activations == [("myplugin", "hooks", "before_llm", "_ReturnHook wrote halted")]


async def test_plugin_hook_returning_none_records_nothing(
    activations: list[tuple[str, str, str, str]],
):
    """Pure observation stays free — a None return is not an activation."""
    with PluginContext("myplugin"):
        register_before_llm(_ReturnHook(None))

    await make_hook_runner("before_llm", default_next="llm")(
        _empty_state(), _empty_runtime(), _empty_config()
    )
    assert activations == []


async def test_framework_hook_records_nothing(activations: list[tuple[str, str, str, str]]):
    """Framework hooks register outside a PluginContext, so they are absent from
    the attribution ledger and from activation telemetry alike."""
    register_before_llm(_ReturnHook({"halted": True}))

    await make_hook_runner("before_llm", default_next="llm")(
        _empty_state(), _empty_runtime(), _empty_config()
    )
    assert activations == []


async def test_activation_key_matches_the_ledger_entry(monkeypatch: pytest.MonkeyPatch):
    """The whole point of reusing `(plugin, surface, identifier)`: an activation
    joins onto the `Contribution` the same registration wrote, with no second
    identifier space to keep in sync."""
    recorded: list[tuple[str, str, str]] = []

    def spy(plugin: str | None, surface: str, identifier: str, *, detail: str = "") -> None:
        if plugin is not None:
            recorded.append((plugin, surface, identifier))

    monkeypatch.setattr(plugin_activation, "record", spy)
    before = len(plugin_contributions.contributions())
    with PluginContext("myplugin"):
        register_before_exec(_ReturnHook({"halted": True}))
    ledger = plugin_contributions.contributions()[before:]

    await make_hook_runner("before_exec", default_next="exec")(
        _empty_state(), _empty_runtime(), _empty_config()
    )
    assert [(c.plugin, c.surface, c.identifier) for c in ledger] == recorded
