"""`ava.state` / `ava.state_update` / `ava.state.<plugin>` namespace view behavior guard.

Plugin <-> framework IPC channel — `agent/graph/_exec.py:_exec_node_impl` runs the
exec in one disposable subprocess: the child rebuilds `ava.state` from the
request-envelope snapshot. Inside the exec the plugin reads
ava.state and writes ava.state_update through the SDK; at the end of the turn
ava.state_update is merged into Command(update=...).
Commit entry validates keys — must be prefixed fields declared through register_plugin_state,
not base field names, not unregistered typos.

Reading ava.state goes through namespace view: `ava.state.<plugin_name>.<field>` auto
de-prefix (`build_agent_state.__getattr__`); writing ava.state_update still uses prefixed
keys (`"<plugin>__<field>"`) because state_update is the LangGraph reducer input.

Test coverage:
- Unit: state.<plugin> namespace view auto strips prefix; unregistered plugin name → AttributeError;
  base fields (messages/halted) do not enter namespace.
- Unit: deepcopy isolation — mutating ava.state.field (list/dict/set/nested) does not affect the original state.
- Integration: runs the real _exec_node_impl path ——
  * happy path: state_update fields go into Command.update
  * cancel path: state_update still merges (load-bearing, T1)
  * lifecycle (terminate): state_update still merges (T3)
  * crash path: try/finally ensures slot reset, state_update still merges
  * plugin writes base field → ValueError (T5, C1)
  * plugin writes unregistered key → ValueError (C2)
  * multiple plugins different keys all merge (T6)
  * plugin accidentally sets ava.state_update = None → TypeError (C3)
  * plugin mutating namespace view does not persist (T7)
"""

import asyncio
import time
from pathlib import Path
from typing import Annotated, Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, RemoveMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph.message import REMOVE_ALL_MESSAGES, add_messages
from langgraph.runtime import Runtime
from pydantic import BaseModel, Field

import ava
from agent.graph._context import AvaContext
from agent.graph._exec import _exec_node_impl
from agent.messages_guard import MessagesMutationError
from agent.state import (
    AttachEntry,
    AttachState,
    BaseAgentState,
    build_agent_state,
    clear_plugin_registrations,
    register_plugin_state,
)
from shared.config.turn_view import bind_agent_config
from shared.plugin_context import PluginContext

assert (
    asyncio
)  # silence "unused" — fixture asyncio.Event implicit; explicit import prevents ruff removal


@pytest.fixture(autouse=True)
def _reset_state_slot():
    """Before/after each test, force reset slots back to None — do not restore previous (would mask test
    leaks). Module-level slots should default to None; any leftover value is a bug."""
    ava.state = None
    ava.state_update = None
    clear_plugin_registrations()
    yield
    ava.state = None
    ava.state_update = None
    clear_plugin_registrations()
    # The in-memory security-findings buffer is process-global; a failed test
    # must not leak findings into the next test's exec.
    import ava.security as _security

    _security._pending_findings = []


# ── Unit: reducer-delta accumulation (2026-08-08 audit, cc-backend-runtime P1) ──


def _set_union(current: set[str], new: set[str]) -> set[str]:
    return current | new


def test_handle_update_accumulates_reducer_deltas_within_turn():
    """Two update() calls to the SAME reducer field in one turn must both reach
    the committed delta: read() inside the turn saw both, and the turn-end
    commit must too. The pre-fix raw-overwrite committed only the last delta
    ({"b"}), silently dropping {"a"} from the checkpoint — the docstring's own
    example pattern (seen: Annotated[set, _set_union])."""

    class _PluginState(BaseModel):
        seen: Annotated[set[str], _set_union] = Field(default_factory=set)

    with PluginContext("delta_test"):
        handle = register_plugin_state(_PluginState)

    ava.state = MagicMock()
    ava.state_update = {}
    # Model the working copy the exec node installed: channel key
    # "delta_test__seen" starts as the checkpoint value.
    ava.state.delta_test__seen = set()

    handle.update({"seen": {"a"}})
    handle.update({"seen": {"b"}})

    assert handle.read().seen == {"a", "b"}  # working copy sees both
    assert ava.state_update["delta_test__seen"] == {"a", "b"}, (
        "committed delta must carry every update of the turn, not just the last"
    )


def test_handle_update_last_value_field_still_collapses_to_latest():
    """A plain (no-reducer) field keeps last-value semantics — the accumulation
    is a no-op for overwrite reducers."""

    class _PluginState(BaseModel):
        counter: int = 0

    with PluginContext("delta_test"):
        handle = register_plugin_state(_PluginState)

    ava.state = MagicMock()
    ava.state_update = {}
    ava.state.delta_test__counter = 0

    handle.update({"counter": 1})
    handle.update({"counter": 2})

    assert ava.state_update["delta_test__counter"] == 2
    assert handle.read().counter == 2


def test_handle_update_rejects_targeted_removal_of_checkpoint_message():
    """The append-only invariant (user ruling 2026-08-13, task #1256): a
    plugin may append, wipe (REMOVE_ALL), or modify the last message — it may
    NOT delete an older message. The working-copy merge runs the guarded
    reducer, so the violation fails inside the exec turn (before any commit).

    (This test used to pin the raw accumulation semantics for a targeted
    RemoveMessage; the ruling made that shape illegal, and the accumulation
    machinery is now covered by the REMOVE_ALL test below and the appends
    test after it.)"""

    m1 = HumanMessage(content="a", id="m1")
    m2 = HumanMessage(content="b", id="m2")

    class _PluginState(BaseModel):
        messages: Annotated[list[AnyMessage], add_messages] = Field(default_factory=list)

    with PluginContext("delta_test"):
        handle = register_plugin_state(_PluginState)

    ava.state = MagicMock()
    ava.state_update = {}
    # Checkpoint value the exec node installed into the working copy.
    ava.state.messages = [m1, m2]

    # Appending is fine...
    handle.update({"messages": [HumanMessage(content="c", id="m3")]})
    assert handle.read().messages[-1].id == "m3"
    # ...but removing an older checkpoint message must fail fast.
    with pytest.raises(MessagesMutationError):
        handle.update({"messages": [RemoveMessage(id="m1")]})


def test_handle_update_remove_all_marker_survives_accumulation():
    """REMOVE_ALL must not be absorbed by the accumulation: applied to the
    accumulated delta it wipes the delta itself — the commit then sees 'no
    update' and the checkpoint keeps its messages while read() saw an empty
    list (silent divergence). Concatenation keeps the marker, and the commit
    removes everything."""

    m1 = HumanMessage(content="a", id="m1")
    m2 = HumanMessage(content="b", id="m2")
    m3 = HumanMessage(content="c", id="m3")

    class _PluginState(BaseModel):
        messages: Annotated[list[AnyMessage], add_messages] = Field(default_factory=list)

    with PluginContext("delta_test"):
        handle = register_plugin_state(_PluginState)

    ava.state = MagicMock()
    ava.state_update = {}
    ava.state.messages = [m1, m2]

    handle.update({"messages": [m3]})
    handle.update({"messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES)]})

    acc = ava.state_update["messages"]
    assert acc == [m3, RemoveMessage(id=REMOVE_ALL_MESSAGES)], (
        "REMOVE_ALL must ride along in the accumulated delta"
    )
    assert handle.read().messages == []
    assert add_messages([m1, m2], acc) == []


def test_handle_update_add_messages_plain_appends_unchanged():
    """Append-only add_messages deltas keep the pre-fix behavior: the
    accumulated delta is the concatenated appends, and the commit reproduces
    the working copy."""

    m1 = HumanMessage(content="a", id="m1")
    m2 = HumanMessage(content="b", id="m2")

    class _PluginState(BaseModel):
        messages: Annotated[list[AnyMessage], add_messages] = Field(default_factory=list)

    with PluginContext("delta_test"):
        handle = register_plugin_state(_PluginState)

    ava.state = MagicMock()
    ava.state_update = {}
    ava.state.messages = []

    handle.update({"messages": [m1]})
    handle.update({"messages": [m2]})

    acc = ava.state_update["messages"]
    assert acc == [m1, m2]
    assert handle.read().messages == [m1, m2]
    assert add_messages([], acc) == [m1, m2]


# ── Core-key whitelist: only `messages` is plugin-writable (Task #1159) ─────


@pytest.mark.parametrize(
    "bad_field",
    [
        "halted",
        "update_initiated",
        "compact",
        "memory",
        "context_reset",
        "capabilities",
    ],
)
def test_register_plugin_state_rejects_non_messages_core_key(bad_field):
    """Declaring any BaseAgentState core key other than `messages` is rejected
    at registration with a clear error — those fields are framework-managed
    every turn. Pre-fix this silently registered and let the plugin write
    (and clobber) the core channel."""

    ns: dict[str, Any] = {
        "__annotations__": {bad_field: bool},
    }
    bad_cls = type(f"_BadState_{bad_field}", (BaseModel,), ns)

    with (
        pytest.raises(ValueError, match=rf"core state field '{bad_field}'"),
        PluginContext("delta_test"),
    ):
        register_plugin_state(bad_cls)


def test_register_plugin_state_still_allows_messages():
    """`messages` stays on the writable whitelist (with the exact base
    annotation) — the whitelist shrinks the core surface to one key, it does
    not remove it."""

    class _PluginState(BaseModel):
        messages: Annotated[list[AnyMessage], add_messages] = Field(default_factory=list)

    with PluginContext("delta_test"):
        handle = register_plugin_state(_PluginState)
    assert handle._channel_keys["messages"] == "messages"


async def test_exec_node_merges_plugin_messages_with_framework_toolmessage(
    fake_cancel_event,
):
    """A plugin that declared `messages` and wrote it this turn must not lose
    the exec ToolMessage: merge_exec_notes combines both deltas into the
    Command — the exec result FIRST, plugin notes after (Anthropic-compat
    wire contract: tool_use must be immediately followed by tool_result, a
    note in between 400s; see _exec_notes.py) — so the checkpoint keeps both.
    Pre-fix the dict **spread let the plugin's delta replace the framework's,
    silently dropping the ToolMessage."""

    # The plugin declares the base `messages` field — the whitelisted core key —
    # so its messages delta passes _validate_plugin_state_keys and reaches the
    # exec node's merge.
    class _MessagesPluginState(BaseModel):
        messages: Annotated[list[AnyMessage], add_messages] = Field(default_factory=list)

    with PluginContext("delta_test"):
        register_plugin_state(_MessagesPluginState)

    class _StateWithPlugin(BaseAgentState):
        plugin__sentinel: bool = False

    code = (
        "import ava\n"
        "from langchain_core.messages import HumanMessage\n"
        "ava.state_update['messages'] = [HumanMessage(content='plugin note', id='p1')]\n"
    )
    state = _StateWithPlugin(
        messages=[_ai_message_with_code(code)],
        halted=False,
        plugin__sentinel=True,
    )
    runtime, config = _make_runtime_and_config(AsyncMock())

    cmd = await _exec_node_impl(cast(BaseAgentState, state), runtime, config)

    update = cast(dict[str, Any], cmd.update)
    msgs = update["messages"]
    assert len(msgs) == 2, f"expected ToolMessage + plugin message, got {msgs!r}"
    assert msgs[0].type == "tool", f"exec ToolMessage must be kept and FIRST: {msgs[0]!r}"
    assert msgs[0].tool_call_id == "call_1"
    assert msgs[1].id == "p1"
    assert msgs[1].content == "plugin note"


# ── Unit: state.<plugin> namespace view ───────────────────────────────────


def test_namespace_view_strips_prefix():
    """ava.state.<plugin_name> exposes `<plugin>__field` de-prefixed to `.field`,
    base fields (messages/halted) do not enter namespace (they are read directly via ava.state)."""

    class AvaCode(BaseModel):
        injected_paths: set[str] = Field(default_factory=set)
        token_count: int = 0

    class OtherPlugin(BaseModel):
        data: str = ""

    with PluginContext("ava_code"):
        register_plugin_state(AvaCode)
    with PluginContext("other_plugin"):
        register_plugin_state(OtherPlugin)

    state_cls = build_agent_state()
    # dynamic AgentState fields (`<plugin>__<field>`) are generated at runtime by register_plugin_state,
    # pyright statically sees state_cls as BaseAgentState — use dict[Any, Any] + **spread to bypass
    # pyright keyword-arg check.
    plugin_fields: dict = {
        "ava_code__injected_paths": {"/a/AGENTS.md", "/b/AGENTS.md"},
        "ava_code__token_count": 42,
        "other_plugin__data": "x",
    }
    ava.state = state_cls(messages=[], halted=False, **plugin_fields)  # pyright: ignore[reportUnknownArgumentType]

    ns = ava.state.ava_code  # type: ignore[attr-defined]
    assert ns.injected_paths == {"/a/AGENTS.md", "/b/AGENTS.md"}  # pyright: ignore[reportUnknownMemberType]
    assert ns.token_count == 42  # pyright: ignore[reportUnknownMemberType]
    # other_plugin's fields do not appear (cross-plugin isolation)
    assert not hasattr(ns, "data")  # pyright: ignore[reportUnknownArgumentType]
    # base fields also do not enter namespace
    assert not hasattr(ns, "messages")  # pyright: ignore[reportUnknownArgumentType]
    assert not hasattr(ns, "halted")  # pyright: ignore[reportUnknownArgumentType]


def test_namespace_view_typo_name_raises_attribute_error():
    """Unregistered plugin name → AttributeError listing known plugin names — more fail-fast
    than silently returning an empty namespace, a typo in plugin name blows up immediately
    instead of later ns.foo AttributeError losing the root cause."""

    class Foo(BaseModel):
        x: int = 0

    class Bar(BaseModel):
        y: str = ""

    with PluginContext("ava_code"):
        register_plugin_state(Foo)
    with PluginContext("other"):
        register_plugin_state(Bar)

    state_cls = build_agent_state()
    ava.state = state_cls(messages=[], halted=False)

    with pytest.raises(AttributeError, match=r"ava_code.*other"):
        _ = ava.state.nonexistent  # type: ignore[attr-defined]


def test_namespace_view_mutation_does_not_persist():
    """namespace is a by-value SimpleNamespace copy — mutating ns.field does not reflect to
    ava.state, nor commit into the next turn. To persist must use ava.state_update[...]."""

    class P(BaseModel):
        counter: int = 0

    with PluginContext("plugin"):
        register_plugin_state(P)

    state_cls = build_agent_state()
    plugin_fields: dict = {"plugin__counter": 1}
    ava.state = state_cls(messages=[], halted=False, **plugin_fields)  # pyright: ignore[reportUnknownArgumentType]

    ns = ava.state.plugin  # type: ignore[attr-defined]
    assert ns.counter == 1  # pyright: ignore[reportUnknownMemberType]
    ns.counter = 999  # mutate namespace by-value copy
    # original state unchanged (namespace and state are independent Python objects)
    assert ava.state.plugin.counter == 1  # type: ignore[attr-defined]
    assert ava.state.plugin__counter == 1  # type: ignore[attr-defined]
    # state_update also not auto-chased — plugin must be explicit
    assert ava.state_update is None


# ── Unit: deepcopy isolation (list / dict / set / nested) ───────────────────────


def test_deepcopy_isolates_list_field():
    """`model_copy(deep=True)` does recursive copy for list fields."""

    class _State(BaseAgentState):
        plugin__tags: list[str] = Field(default_factory=list)

    real = _State(messages=[], halted=False, plugin__tags=["a", "b"])
    snapshot = real.model_copy(deep=True)
    snapshot.plugin__tags.append("c")
    assert real.plugin__tags == ["a", "b"]


def test_deepcopy_isolates_dict_field():
    """dict fields recursive copy — mutating snapshot dict does not affect real."""

    class _State(BaseAgentState):
        plugin__counts: dict[str, int] = Field(default_factory=dict)

    real = _State(messages=[], halted=False, plugin__counts={"k": 1})
    snapshot = real.model_copy(deep=True)
    snapshot.plugin__counts["k"] = 99
    snapshot.plugin__counts["new"] = 42
    assert real.plugin__counts == {"k": 1}


def test_deepcopy_isolates_set_field():
    """set fields recursive copy."""

    class _State(BaseAgentState):
        plugin__seen: set[str] = Field(default_factory=set)

    real = _State(messages=[], halted=False, plugin__seen={"x"})
    snapshot = real.model_copy(deep=True)
    snapshot.plugin__seen.add("y")
    assert real.plugin__seen == {"x"}


def test_deepcopy_isolates_nested_pydantic_model():
    """Nested BaseModel fields are also deep recursive copies."""

    class _Inner(BaseModel):
        name: str

    class _State(BaseAgentState):
        plugin__inner: _Inner = Field(default_factory=lambda: _Inner(name="orig"))

    real = _State(messages=[], halted=False, plugin__inner=_Inner(name="orig"))
    snapshot = real.model_copy(deep=True)

    snapshot.plugin__inner.name = "mutated"

    assert real.plugin__inner.name == "orig"


# ── Integration: exec_node full path ────────────────────────────────────────


def _make_runtime_and_config(redis_client: AsyncMock) -> tuple[Runtime[AvaContext], RunnableConfig]:
    ctx = AvaContext(
        ops_pool=None,
        llm=MagicMock(),
        event_publisher=MagicMock(),
    )
    runtime = Runtime(context=ctx)
    config: RunnableConfig = {"configurable": {"thread_id": "42"}}
    return runtime, config


def _ai_message_with_code(code: str) -> AIMessage:
    """Build an AIMessage with an execute_code tool_call — required by exec_node entry."""
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "execute_code",
                "args": {"code": code},
                "id": "call_1",
                "type": "tool_call",
            }
        ],
    )


async def test_exec_node_merges_plugin_state_update_into_command(fake_cancel_event):
    """user code writes ava.state_update[key]=value → Command(update) contains key:value."""

    class _StateWithPlugin(BaseAgentState):
        plugin__counter: int = 0

    state = _StateWithPlugin(
        messages=[_ai_message_with_code("import ava\nava.state_update['plugin__counter'] = 7")],
        halted=False,
        plugin__counter=0,
    )
    runtime, config = _make_runtime_and_config(AsyncMock())

    cmd = await _exec_node_impl(cast(BaseAgentState, state), runtime, config)

    update = cast(dict, cmd.update)
    assert update.get("plugin__counter") == 7  # pyright: ignore[reportUnknownMemberType]
    assert "messages" in update
    assert "halted" in update


async def test_exec_node_resets_state_slot_after_turn(fake_cancel_event):
    """After exec_node exits, ava.state / ava.state_update must be reset to None."""
    state = BaseAgentState(messages=[_ai_message_with_code("pass")], halted=False)
    runtime, config = _make_runtime_and_config(AsyncMock())

    await _exec_node_impl(state, runtime, config)
    assert ava.state is None
    assert ava.state_update is None


async def test_exec_node_resets_state_slot_even_on_crash(fake_cancel_event):
    """user code raises exception → exec_node takes the _ExecCrashed path, not raising —
    slot still reset. Try/finally ensures."""
    state = BaseAgentState(
        messages=[_ai_message_with_code("raise RuntimeError('boom')")],
        halted=False,
    )
    runtime, config = _make_runtime_and_config(AsyncMock())

    cmd = await _exec_node_impl(state, runtime, config)
    assert cmd is not None
    assert ava.state is None
    assert ava.state_update is None


async def test_plugin_cannot_overwrite_base_field_via_ava_state(fake_cancel_event):
    """plugin changing ava.state.halted = True only affects the deepcopy copy — the real
    LangGraph state is unchanged, Command.update.halted is calculated by the framework
    (this _ExecDone case). The focus is not "halted is specially protected" but
    "all writes through ava.state do not persist — through ava.state writing does not enter
    the next turn"."""

    class _StateWithSentinel(BaseAgentState):
        plugin__sentinel: bool = False

    code = (
        # plugin changes deepcopy copy — does not persist
        "import ava\n"
        "ava.state.halted = True\n"
        # plugin explicitly writes state_update[sentinel] — this one commits
        "ava.state_update['plugin__sentinel'] = True\n"
    )
    state = _StateWithSentinel(
        messages=[_ai_message_with_code(code)],
        halted=False,
        plugin__sentinel=False,
    )
    runtime, config = _make_runtime_and_config(AsyncMock())

    cmd = await _exec_node_impl(cast(BaseAgentState, state), runtime, config)

    # real state unchanged
    assert state.halted is False
    # Command update.halted is framework-calculated (_ExecDone hardcodes False), not plugin-injected
    update = cast(dict, cmd.update)
    assert update["halted"] is False
    # but plugin's explicit state_update["plugin__sentinel"] successfully commits
    assert update.get("plugin__sentinel") is True  # pyright: ignore[reportUnknownMemberType]


# ── Integration: state_update key validation fail-fast ───────────────────────────


@pytest.mark.parametrize(
    "base_field",
    # `_BASE_STATE_FIELDS` is derived from BaseAgentState.model_fields (I-8), so
    # the guard covers every base field — not just the original messages/halted
    # but also update_initiated and the nested compact / memory sub-states. A
    # plugin writing any of these via state_update without declaring it raises.
    ["messages", "halted", "update_initiated", "compact", "memory"],
)
async def test_state_update_base_field_key_raises(fake_cancel_event, base_field):
    """plugin missing prefix typo writes base field name into state_update → ValueError
    immediately blows up. Without this check, Python dict literal **spread would overwrite,
    plugin silently clobbers framework's base channel (messages / halted / compact / memory)
    this turn, silently losing exec output or disrupting compact state machine."""

    state = BaseAgentState(
        messages=[_ai_message_with_code(f"import ava\nava.state_update[{base_field!r}] = 'X'")],
        halted=False,
    )
    runtime, config = _make_runtime_and_config(AsyncMock())

    with pytest.raises(ValueError, match=f"base field.*{base_field}"):
        await _exec_node_impl(state, runtime, config)


async def test_state_update_unknown_key_raises(fake_cancel_event):
    """plugin writes unregistered key → ValueError listing known plugin fields, not
    entering Command(update=...) letting LangGraph silently drop."""

    class _State(BaseAgentState):
        plugin__known: int = 0

    state = _State(
        messages=[_ai_message_with_code("import ava\nava.state_update['plugin__typo'] = 1")],
        halted=False,
        plugin__known=0,
    )
    runtime, config = _make_runtime_and_config(AsyncMock())

    with pytest.raises(ValueError, match=r"unregistered key.*plugin__typo"):
        await _exec_node_impl(cast(BaseAgentState, state), runtime, config)


async def test_state_update_multiple_plugins_no_conflict(fake_cancel_event):
    """Two plugins writing different prefixed keys, both enter Command.update — no mutual overwrite."""

    class _State(BaseAgentState):
        plugin_a__x: int = 0
        plugin_b__y: str = ""

    code = "import ava\nava.state_update['plugin_a__x'] = 10\nava.state_update['plugin_b__y'] = 'hello'\n"
    state = _State(messages=[_ai_message_with_code(code)], halted=False)
    runtime, config = _make_runtime_and_config(AsyncMock())

    cmd = await _exec_node_impl(cast(BaseAgentState, state), runtime, config)

    update = cast(dict, cmd.update)
    assert update.get("plugin_a__x") == 10  # pyright: ignore[reportUnknownMemberType]
    assert update.get("plugin_b__y") == "hello"  # pyright: ignore[reportUnknownMemberType]


async def test_state_update_non_dict_raises_type_error(fake_cancel_event):
    """plugin inside the exec child sets ava.state_update to None / list /
    str → TypeError, not silent (`or {}` fallback against CLAUDE.md fail-fast)."""

    state = BaseAgentState(
        messages=[_ai_message_with_code("import ava\nava.state_update = None")],
        halted=False,
    )
    runtime, config = _make_runtime_and_config(AsyncMock())

    with pytest.raises(TypeError, match=r"plugin tampered with ava.state_update"):
        await _exec_node_impl(state, runtime, config)


# ── Integration: cancel / lifecycle paths state_update still merges ──────────
#
# load-bearing invariant——the state delta a plugin writes during a cancel / terminate / restart
# turn (e.g. ava_code__cwd halfway set to another location) MUST still be merged into
# Command.update, must not be lost just because it's not the happy path.
# Currently _exec_node_impl implements capture plugin_state_update **after subscribe_interrupt
# exits**, shared with all _ExecResult branches through the common return Command entry, so these
# paths are all covered——but if untested, a future refactor that moves capture into the cancel case
# would cause a silent regression. The following three tests nail down this invariant.


async def test_exec_node_preserves_state_update_on_cancel(
    fake_cancel_event: asyncio.Event, tmp_path: Path
):
    """In the cancel path, the state_update written by the plugin before the cancel still merges into Command.

    The cancel fires only after the exec code has provably started: a marker
    file the code writes after its state_update (subprocess boot — `import ava`
    plus plugins — takes ~1-2s, so a fixed short delay would race it on CI)."""

    class _State(BaseAgentState):
        plugin__progress: str = ""

    marker = tmp_path / "started.marker"
    # plugin first writes state_update, proves it ran, then dead-waits
    # (sleep long enough to be cancelled)
    code = (
        "import ava\n"
        "ava.state_update['plugin__progress'] = 'before-cancel'\n"
        f"open({str(marker)!r}, 'w').write('x')\n"
        "import time; time.sleep(30)\n"
    )
    state = _State(messages=[_ai_message_with_code(code)], halted=False)
    runtime, config = _make_runtime_and_config(AsyncMock())

    node_task = asyncio.create_task(_exec_node_impl(cast(BaseAgentState, state), runtime, config))
    deadline = time.monotonic() + 20.0
    while time.monotonic() < deadline and not marker.exists():
        await asyncio.sleep(0.05)
    assert marker.exists(), "exec code never started within 20s — cancel would race boot"
    fake_cancel_event.set()
    cmd = await node_task

    update = cast(dict, cmd.update)
    assert update.get("plugin__progress") == "before-cancel"  # pyright: ignore[reportUnknownMemberType]
    assert update["halted"] is True  # cancel path halted=True
    assert ava.state is None
    assert ava.state_update is None


async def test_exec_node_preserves_state_update_on_lifecycle(fake_cancel_event):
    """Lifecycle (terminate) path: plugin's state_update written before raising still merges.

    Directly raise AgentTermination to avoid `ava.self.terminate()`'s internal db INSERT
    (test doesn't mock ava.DB)——the invariant for this test is "plugin state_update in lifecycle
    path still merges into Command", not the full terminate flow."""
    from ava.self import AgentTermination

    assert AgentTermination is not None  # let import not be removed by ruff

    class _State(BaseAgentState):
        plugin__last_action: str = ""

    code = (
        "import ava\n"
        "ava.state_update['plugin__last_action'] = 'about-to-terminate'\n"
        "from ava.self import AgentTermination\n"
        "raise AgentTermination\n"
    )
    state = _State(messages=[_ai_message_with_code(code)], halted=False)
    runtime, config = _make_runtime_and_config(AsyncMock())

    cmd = await _exec_node_impl(cast(BaseAgentState, state), runtime, config)

    update = cast(dict, cmd.update)
    assert update.get("plugin__last_action") == "about-to-terminate"  # pyright: ignore[reportUnknownMemberType]
    assert update["halted"] is True
    assert ava.state is None


# ── exec-side system-note injection (user ruling 2026-08-11) ────────────────
# AGENTS.md / security findings are delivered in-memory inside the exec's
# messages delta — the exec node drains ava.security's in-memory findings
# buffer and merges plugin-contributed messages, both AFTER the exec-result
# ToolMessage (the Anthropic-compat tool_use -> tool_result adjacency
# invariant forbids notes between the AIMessage and its ToolMessage; verified
# against the DeepSeek anthropic endpoint 2026-08-11: "tool_use ids were
# found without tool_result blocks immediately after").


def _seed_security_findings(*sources: str) -> None:
    """Plant findings into ava.security's in-memory buffer, as scan_content
    would have during the exec turn."""
    from ava import security as _security

    _security._pending_findings = [
        _security.SecurityFindingEntry(source=src, triggers=["ignore previous instructions"])
        for src in sources
    ]


def _register_messages_plugin() -> None:
    """Declare the base `messages` channel for a test plugin, mirroring what
    ava_code does in production (user ruling 2026-08-11: context notes ride
    the exec's messages delta)."""

    class _MessagesPluginState(BaseModel):
        messages: Annotated[list[AnyMessage], add_messages] = Field(default_factory=list)

    with PluginContext("delta_test"):
        register_plugin_state(_MessagesPluginState)


async def test_exec_node_injects_security_finding_after_toolmessage(fake_cancel_event):
    """A finding buffered during the exec turn is delivered as a SECURITY
    system note in the same exec's messages delta, after the exec-result
    ToolMessage — no side-channel file."""
    _seed_security_findings("shell.run")

    state = BaseAgentState(
        messages=[_ai_message_with_code("pass")],
        halted=False,
    )
    runtime, config = _make_runtime_and_config(AsyncMock())

    cmd = await _exec_node_impl(state, runtime, config)

    update = cast(dict[str, Any], cmd.update)
    msgs = update["messages"]
    assert len(msgs) == 2, f"expected ToolMessage + security note, got {msgs!r}"
    assert msgs[0].type == "tool", f"exec ToolMessage must come first: {msgs[0]!r}"
    assert msgs[0].tool_call_id == "call_1"
    assert msgs[1].type == "human"
    assert "shell.run" in msgs[1].content
    assert "ignore previous instructions" in msgs[1].content
    # Buffer cleared — findings delivered exactly once
    from ava import security as _security

    assert _security.take_findings() == []


async def test_exec_node_orders_tool_security_then_plugin_notes(fake_cancel_event):
    """Order in the merged delta: exec ToolMessage, then security warnings
    (they annotate the content notes), then the plugin's context notes — the
    tool_use adjacency is preserved and warnings precede the content they
    flag."""
    _register_messages_plugin()
    _seed_security_findings("context-file:/repo/AGENTS.md")

    code = (
        "import ava\n"
        "from langchain_core.messages import HumanMessage\n"
        "ava.state_update['messages'] = [HumanMessage(content='project note', id='p1')]\n"
    )
    state = BaseAgentState(messages=[_ai_message_with_code(code)], halted=False)
    runtime, config = _make_runtime_and_config(AsyncMock())

    cmd = await _exec_node_impl(state, runtime, config)

    update = cast(dict[str, Any], cmd.update)
    msgs = update["messages"]
    assert [m.type for m in msgs] == ["tool", "human", "human"], (
        f"expected [tool, security, plugin], got {[m.type for m in msgs]}"
    )
    assert "context-file:/repo/AGENTS.md" in msgs[1].content
    assert msgs[2].id == "p1"
    assert msgs[2].content == "project note"


async def test_exec_node_checkpoints_child_attachment(fake_cancel_event, tmp_path: Path):
    """A normal child registration drains into a media message in the exec update.

    User ruling 2026-08-26: the attach message lands right after the exec
    output in the SAME turn, so the update must contain the packed media
    HumanMessage and a cleared attach channel — not parked pending entries
    for the claim boundary.
    """
    from shared.message_kwargs import AvaMsgType

    # The real exec child rejects attach for a text-only model (user ruling
    # 2026-08-28) — boot it with a media-capable model via the per-agent
    # config map the exec path re-emits into the child env (a bare home's
    # env-authority pass drops an inherited AVA_MODEL).
    image = tmp_path / "render.png"
    image.write_bytes(b"png")
    code = f"import ava\nava.self.attach({str(image)!r}, label='render result')"
    state = BaseAgentState(messages=[_ai_message_with_code(code)], halted=False)
    runtime, config = _make_runtime_and_config(AsyncMock())

    with bind_agent_config({"llm_model": "deepseek-v4-flash-vision-exp"}):
        cmd = await _exec_node_impl(state, runtime, config)

    update = cast(dict[str, Any], cmd.update)
    # Pending is drained (cleared) in the same update — nothing parked.
    assert update["attach"] == AttachState()
    messages = update["messages"]
    assert len(messages) == 2
    attach_msg = messages[-1]
    assert isinstance(attach_msg, HumanMessage)
    assert attach_msg.additional_kwargs["ava_msg_type"] == AvaMsgType.ATTACH.value  # pyright: ignore[reportUnknownMemberType]
    # Interleaved pack: the notice leads, then the file's own caption line.
    # The exec-node context model here is a bare MagicMock (no media
    # capability), so the pack is caption-only — no media block.
    content = attach_msg.content  # pyright: ignore[reportUnknownMemberType]
    assert isinstance(content, list)
    assert [cast("dict[str, Any]", b)["type"] for b in content] == ["text", "text"]
    assert "Files attached during this turn" in cast("dict[str, Any]", content[0])["text"]
    caption_block = cast("dict[str, Any]", content[1])
    assert "render.png" in caption_block["text"]


async def test_exec_node_compact_path_drops_notes_and_clears_findings(
    fake_cancel_event, tmp_path: Path
):
    """The compact path (_SystemHalt) writes nothing back — claim REMOVE_ALLs
    the whole history — so notes must not leak into the update, and the
    findings buffer must still be cleared (never misattributed to a later
    turn)."""

    _register_messages_plugin()
    _seed_security_findings("shell.run")
    # The real exec child rejects attach for a text-only model (user ruling
    # 2026-08-28) — boot it with a media-capable model via the per-agent
    # config map the exec path re-emits into the child env (a bare home's
    # env-authority pass drops an inherited AVA_MODEL).
    image = tmp_path / "render.png"
    image.write_bytes(b"png")
    code = (
        "import ava\n"
        "from langchain_core.messages import HumanMessage\n"
        "ava.state_update['messages'] = [HumanMessage(content='x')]\n"
        f"ava.self.attach({str(image)!r})\n"
        "from shared.lifecycle import _SystemHalt\n"
        "raise _SystemHalt()\n"
    )
    state = BaseAgentState(
        messages=[_ai_message_with_code(code)],
        halted=False,
        attach=AttachState(pending=[AttachEntry(path="/previous.png", label=None)]),
    )
    runtime, config = _make_runtime_and_config(AsyncMock())

    with bind_agent_config({"llm_model": "deepseek-v4-flash-vision-exp"}):
        cmd = await _exec_node_impl(state, runtime, config)

    update = cast(dict, cmd.update)
    assert update.get("messages") == [], (  # pyright: ignore[reportUnknownMemberType]
        f"compact path must write no messages back, got {update.get('messages')!r}"  # pyright: ignore[reportUnknownMemberType]
    )
    assert update.get("halted") is True  # pyright: ignore[reportUnknownMemberType]
    assert "attach" not in update
    # Findings drained even though nothing was injected
    from ava import security as _security

    assert _security.take_findings() == []
