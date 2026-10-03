"""`plugin_state_schema` / `build_agent_state(extensions)`: plugin state is a declaration, validated
and turned into a class per registry — nothing process-global is mutated by a declaration.

The core-key rejection (every BaseAgentState field other than `messages`) is covered next to the
exec-node tests in `test_state_slot.py`.
"""

from typing import Annotated, Any, cast

import pytest
from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages
from pydantic import BaseModel, Field

import agent.state as agent_state
from agent.state import (
    BaseAgentState,
    PluginStateHandle,
    _validate_plugin_state_keys,
    build_agent_state,
    checkpoint_msgpack_allowlist,
    plugin_state_schema,
)
from base.packages.plugins.extensions import ExtensionRegistry, PluginContributions


@pytest.fixture(autouse=True)
def _restore_agent_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """`build_agent_state` rebinds `agent.state.AgentState`; put it back after each test."""
    monkeypatch.setattr(agent_state, "AgentState", BaseAgentState)


def _declared(state_cls: type[BaseAgentState], name: str) -> Any:
    """A class attribute `build_agent_state` stamps on the class it builds."""
    return cast(Any, state_cls).__dict__[name]


def _registry(*plugins: tuple[str, type[BaseModel]]) -> ExtensionRegistry:
    return ExtensionRegistry(
        tuple((name, PluginContributions(state=(cls,))) for name, cls in plugins)
    )


class _Private(BaseModel):
    counter: int = 0
    tags: set[str] = Field(default_factory=set)


class _MessagesDeclaring(BaseModel):
    messages: Annotated[list[AnyMessage], add_messages] = Field(default_factory=list)
    note: str = ""


def test_private_fields_get_the_plugin_prefix_and_the_namespace_view_works() -> None:
    state_cls = build_agent_state(_registry(("demo", _Private)))

    assert {"demo__counter", "demo__tags"} <= set(state_cls.model_fields)
    assert "counter" not in state_cls.model_fields
    state = state_cls(demo__counter=3, demo__tags={"a"})  # pyright: ignore[reportCallIssue]
    ns: Any = cast(Any, state).demo
    assert (ns.counter, ns.tags) == (3, {"a"})
    with pytest.raises(AttributeError, match="demo"):
        _ = cast(Any, state).other


def test_a_registry_without_state_builds_the_base_class() -> None:
    assert build_agent_state(ExtensionRegistry()) is BaseAgentState


def test_a_non_messages_core_field_raises() -> None:
    class _Halting(BaseModel):
        halted: bool = False

    with pytest.raises(ValueError, match="core state field 'halted'"):
        plugin_state_schema(_registry(("demo", _Halting)))


def test_a_mismatched_messages_annotation_raises() -> None:
    class _WrongMessages(BaseModel):
        messages: list[str] = Field(default_factory=list)

    with pytest.raises(ValueError, match="base field 'messages'"):
        plugin_state_schema(_registry(("demo", _WrongMessages)))


def test_a_declared_state_that_is_not_a_basemodel_raises() -> None:
    class _NotAModel:
        counter: int = 0

    registry = ExtensionRegistry(
        (("demo", PluginContributions(state=(_NotAModel,))),)  # pyright: ignore[reportArgumentType]
    )
    with pytest.raises(TypeError, match="not a BaseModel subclass"):
        plugin_state_schema(registry)


def test_two_registries_build_independent_classes() -> None:
    first = build_agent_state(_registry(("first", _Private)))
    second = build_agent_state(_registry(("second", _MessagesDeclaring)))

    assert "first__counter" in first.model_fields
    assert not any(name.startswith("first__") for name in second.model_fields)
    assert "second__note" in second.model_fields
    assert not any(name.startswith("second__") for name in first.model_fields)
    assert set(_declared(first, "__plugin_namespace_fields__")) == {"first"}
    assert set(_declared(second, "__plugin_namespace_fields__")) == {"second"}
    assert _declared(first, "__plugin_base_declared__") == frozenset()
    assert _declared(second, "__plugin_base_declared__") == frozenset({"messages"})
    assert _declared(first, "__plugin_state_classes__") == frozenset({_Private})
    assert _declared(second, "__plugin_state_classes__") == frozenset({_MessagesDeclaring})
    # The first class still answers for its own plugin set after the second was built.
    state = first(first__counter=1)  # pyright: ignore[reportCallIssue]
    assert cast(Any, state).first.counter == 1


def test_state_key_validation_reads_the_classs_own_declared_base_fields() -> None:
    declaring = build_agent_state(_registry(("demo", _MessagesDeclaring)))
    plain = build_agent_state(_registry(("demo", _Private)))

    assert _validate_plugin_state_keys({"messages": []}, declaring) == {"messages": []}
    with pytest.raises(ValueError, match="undeclared base field"):
        _validate_plugin_state_keys({"messages": []}, plain)
    # A framework-managed core key is never writable, declared or not.
    with pytest.raises(ValueError, match="undeclared base field"):
        _validate_plugin_state_keys({"halted": True}, declaring)
    # A prefixed private field of the class's own plugin passes; a typo does not.
    assert _validate_plugin_state_keys({"demo__counter": 1}, plain) == {"demo__counter": 1}
    with pytest.raises(ValueError, match="unregistered key"):
        _validate_plugin_state_keys({"demo__typo": 1}, plain)


def test_the_handle_routes_base_fields_bare_and_private_fields_prefixed() -> None:
    handle = PluginStateHandle(_MessagesDeclaring, "demo")

    assert handle._channel_keys == {"messages": "messages", "note": "demo__note"}


def test_the_allowlist_includes_a_plugin_class_only_when_passed_it() -> None:
    entry = (_Private.__module__, _Private.__name__)

    assert entry not in checkpoint_msgpack_allowlist()
    assert entry in checkpoint_msgpack_allowlist((_Private,))
    # The classes a built AgentState carries are what a serializer in another process gets.
    state_cls = build_agent_state(_registry(("demo", _Private)))
    assert entry in checkpoint_msgpack_allowlist(_declared(state_cls, "__plugin_state_classes__"))
    assert agent_state.process_state_classes() == frozenset({_Private})
