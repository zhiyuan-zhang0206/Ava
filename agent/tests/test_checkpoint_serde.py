"""Checkpoint msgpack allowlist — `agent.state.checkpoint_msgpack_allowlist`.

LangGraph's `JsonPlusSerializer` deserializes pydantic-v2 channel values through
an explicit type allowlist. Without one it runs permissive and warns on **every**
checkpoint load for each unregistered type ("Deserializing unregistered type
agent.state.* from checkpoint... This will be blocked in a future version") —
one line per type per process start. The framework's saver (`services/agent_runner/agent_host/daemon.py`)
now passes `allowed_msgpack_modules=checkpoint_msgpack_allowlist()`.

Guards:
- the five nested sub-states round-trip silently under the allowlist serde
  (and warn under the default permissive serde — the regression this fixes);
- plugin classes a caller passes in (the classes a registry declares) enter the
  allowlist (a plugin field holding a BaseModel instance crosses the
  checkpointer as that class);
- unknown types degrade to their raw dict under the allowlist (documented
  behavior — the permissive default is a future break, the allowlist is not);
- safe types (builtins.set, langchain messages) stay untouched.
"""

import logging

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from pydantic import BaseModel

from agent.state import (
    AttachEntry,
    AttachState,
    CapabilitiesState,
    CompactState,
    ContextReset,
    MemoryState,
    SkillRecallState,
    build_checkpoint_serde,
    checkpoint_msgpack_allowlist,
)
from base.agents.history.checkpoint_serializer import PreSplitCheckpointEnvelopeError

_SERDE_LOGGER = "langgraph.checkpoint.serde.jsonplus"


def _allowlist_serde() -> JsonPlusSerializer:
    return JsonPlusSerializer(allowed_msgpack_modules=checkpoint_msgpack_allowlist())


def _warning_messages(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        r.message
        for r in caplog.get_records("call")
        if r.name == _SERDE_LOGGER and r.levelno >= logging.WARNING
    ]


# ── the five nested sub-states round-trip silently under the allowlist ────


@pytest.mark.parametrize(
    "state",
    [
        AttachEntry(path="/example/render.png", label="render"),
        AttachState(pending=[AttachEntry(path="/example/render.png", label="render")]),
        CompactState(version=3, reminder_shown=True, reminder_seen_version=2),
        MemoryState(injected_paths={"a.md", "b.md"}, window=2),
        ContextReset(resume="claim"),
        CapabilitiesState(indexed={"meiva.skills.gmail", "meiva.skills.wechat"}),
        SkillRecallState(prompted={"web-ai:deep-research"}, window=2),
    ],
)
def test_allowlist_round_trip_is_silent(state, caplog) -> None:
    serde = _allowlist_serde()
    with caplog.at_level(logging.WARNING, logger=_SERDE_LOGGER):  # pyright: ignore[reportUnknownMemberType]
        restored = serde.loads_typed(serde.dumps_typed(state))
    assert type(restored) is type(state)  # pyright: ignore[reportUnknownArgumentType]
    assert restored == state
    assert _warning_messages(caplog) == []  # pyright: ignore[reportUnknownArgumentType]


def test_default_permissive_serde_warns_for_same_types(
    caplog, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The regression: the default serde (no allowlist) warns once per type."""
    # LangGraph dedups warnings in a process-global set
    # (`_warned_unregistered_types`); a sibling test that round-tripped one of
    # these types through a permissive serde first would prime it and this
    # test would see fewer than 4 warnings — order-dependent (issue #161).
    import langgraph.checkpoint.serde.jsonplus as _jsonplus

    monkeypatch.setattr(
        _jsonplus,
        "_warned_unregistered_types",
        set[tuple[str, str]](),  # same element type as the module-level memo
    )
    serde = JsonPlusSerializer()
    states = [
        AttachEntry(path="/example/render.png", label=None),
        AttachState(),
        ContextReset(),
        CapabilitiesState(),
        CompactState(),
        MemoryState(),
    ]
    with caplog.at_level(logging.WARNING, logger=_SERDE_LOGGER):  # pyright: ignore[reportUnknownMemberType]
        for s in states:
            serde.loads_typed(serde.dumps_typed(s))
    msgs = _warning_messages(caplog)  # pyright: ignore[reportUnknownArgumentType]
    assert len(msgs) == 6
    # Issue #156 split: freshly-written checkpoints carry the models' real module
    # (agent.state_channels); the legacy agent.state names only appear in
    # checkpoints written before the split.
    for name in (
        "AttachEntry",
        "AttachState",
        "ContextReset",
        "CapabilitiesState",
        "CompactState",
        "MemoryState",
    ):
        assert any(f"agent.state_channels.{name}" in m for m in msgs)


# ── plugin classes enter the allowlist automatically ──────────────────────


def test_allowlist_covers_declared_plugin_state_classes() -> None:
    class MyPluginState(BaseModel):
        counter: int = 0

    allow = checkpoint_msgpack_allowlist((MyPluginState,))
    assert (MyPluginState.__module__, MyPluginState.__name__) in allow


def test_allowlist_omits_plugin_state_classes_not_passed() -> None:
    class MyPluginState(BaseModel):
        counter: int = 0

    allow = checkpoint_msgpack_allowlist()
    assert (MyPluginState.__module__, MyPluginState.__name__) not in allow


def test_allowlist_static_entries_always_present() -> None:
    allow = checkpoint_msgpack_allowlist()
    for name in (
        "CompactState",
        "AttachState",
        "AttachEntry",
        "MemoryState",
        "ContextReset",
        "CapabilitiesState",
    ):
        assert ("agent.state_channels", name) in allow


def test_pre_split_agent_state_envelopes_fail_the_load() -> None:
    """A checkpoint naming `("agent.state", ...)` predates the channel split and is refused.

    Simulated with a subclass whose `__module__` is the pre-split value (the
    envelope writes `__class__.__module__`). The stock allowlist serde would
    silently hand back a raw dict; the framework serializer raises instead.
    """
    serde = build_checkpoint_serde()
    pre_split = type("CompactState", (CompactState,), {"__module__": "agent.state"})
    blob = serde.dumps_typed(pre_split(version=2))
    with pytest.raises(PreSplitCheckpointEnvelopeError, match="pre-split"):
        serde.loads_typed(blob)
    nested = serde.dumps_typed({"channel": pre_split(version=2), "other": [1, 2]})
    with pytest.raises(PreSplitCheckpointEnvelopeError):
        serde.loads_typed(nested)
    assert not {pair for pair in checkpoint_msgpack_allowlist() if pair[0] == "agent.state"}


def test_current_envelopes_and_lookalike_text_load_normally() -> None:
    serde = build_checkpoint_serde()
    state = CompactState(version=2)
    assert serde.loads_typed(serde.dumps_typed(state)) == state
    # A string that happens to be exactly the legacy module name is data, not an envelope.
    assert serde.loads_typed(serde.dumps_typed({"note": "agent.state"})) == {"note": "agent.state"}


def test_static_allowlist_covers_all_nested_substates() -> None:
    """Hard guard for the allowlist contract: every nested sub-state — a
    BaseAgentState field whose value is a BaseModel instance — must be named
    in `base/agents/history/checkpoint_serde.py::STATIC_CHECKPOINT_MSGPACK_TYPES`. A new
    sub-state added without registration deserializes as a raw dict the
    moment the permissive default is gone (fails loudly on load, but the
    channel is already broken); this test makes the miss fail at CI time
    instead.

    Imported from agent.state directly so the set is discovered from the
    schema, not duplicated here.
    """
    from pydantic import BaseModel as PydanticBaseModel

    from agent.state import BaseAgentState
    from base.agents.history.checkpoint_serde import STATIC_CHECKPOINT_MSGPACK_TYPES

    nested: list[tuple[str, str]] = []
    for field in BaseAgentState.model_fields.values():
        annotation = field.annotation
        if isinstance(annotation, type) and issubclass(annotation, PydanticBaseModel):
            nested.append((annotation.__module__, annotation.__name__))
    assert nested, "expected at least one nested sub-state in BaseAgentState"
    for key in nested:
        assert key in STATIC_CHECKPOINT_MSGPACK_TYPES, (
            f"nested sub-state {key} missing from "
            f"base/agents/history/checkpoint_serde.py::STATIC_CHECKPOINT_MSGPACK_TYPES — add it "
            f"or the channel degrades to a raw dict on checkpoint load"
        )


# ── unknown types degrade to raw dict under the allowlist (documented) ────


def test_allowlist_blocks_unregistered_type_to_dict(caplog) -> None:
    class UnknownState(BaseModel):
        x: int = 1

    serde = _allowlist_serde()
    with caplog.at_level(logging.WARNING, logger=_SERDE_LOGGER):  # pyright: ignore[reportUnknownMemberType]
        restored = serde.loads_typed(serde.dumps_typed(UnknownState(x=5)))
    # blocked → the raw kwargs dict, plus a "Blocked deserialization" warning
    assert restored == {"x": 5}
    assert any("Blocked deserialization" in m for m in _warning_messages(caplog))  # pyright: ignore[reportUnknownArgumentType]


# ── safe types are untouched by the allowlist ─────────────────────────────


def test_allowlist_keeps_set_and_messages(caplog) -> None:
    serde = _allowlist_serde()
    with caplog.at_level(logging.WARNING, logger=_SERDE_LOGGER):  # pyright: ignore[reportUnknownMemberType]
        restored_set = serde.loads_typed(serde.dumps_typed({"a", "b"}))
        restored_msgs = serde.loads_typed(
            serde.dumps_typed([HumanMessage(content="hi"), AIMessage(content="yo")])
        )
    assert restored_set == {"a", "b"}
    assert [type(m) for m in restored_msgs] == [HumanMessage, AIMessage]
    assert _warning_messages(caplog) == []  # pyright: ignore[reportUnknownArgumentType]
