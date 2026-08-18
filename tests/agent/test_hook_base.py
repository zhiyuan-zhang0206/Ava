"""Runtime contract of the `Hook` base class.

The *static* half of the contract — that pyright rejects an incompatible
`__call__` override — is proven in `hook_typing_contract.py` (a pyright-only
module; see its docstring). This file covers the runtime half: ABC enforcement
(can't instantiate an abstract hook), callability, and the `name` diagnostic
label.
"""

from unittest.mock import MagicMock

from langchain_core.runnables import RunnableConfig
from langgraph.runtime import Runtime

from agent.graph._context import AvaContext
from agent.hooks import Hook
from agent.state import AgentState
from tests.agent._fakes import make_fake_ops_pool


class _OkHook(Hook):
    async def __call__(
        self,
        state: AgentState,
        runtime: Runtime[AvaContext],
        config: RunnableConfig,
        /,
    ) -> dict | None:
        return {"halted": True}


class _NamedHook(Hook):
    @property
    def name(self) -> str:
        return "custom-label"

    async def __call__(
        self,
        state: AgentState,
        runtime: Runtime[AvaContext],
        config: RunnableConfig,
        /,
    ) -> dict | None:
        return None


def _runtime() -> Runtime[AvaContext]:
    ctx = AvaContext(
        ops_pool=make_fake_ops_pool(),
        llm=MagicMock(),
        event_publisher=MagicMock(),
    )
    return Runtime(context=ctx)


def test_base_hook_cannot_be_instantiated():
    """`Hook` has an abstract `__call__` — direct instantiation raises TypeError.
    The `type` alias erases abstractness for pyright while keeping the runtime
    class object."""
    cls: type = Hook
    try:
        cls()
    except TypeError:
        return
    raise AssertionError("instantiating abstract Hook should raise TypeError")


def test_subclass_without_call_cannot_be_instantiated():
    """A subclass that forgets to override `__call__` is still abstract."""

    class _Incomplete(Hook):
        pass

    cls: type = _Incomplete
    try:
        cls()
    except TypeError:
        return
    raise AssertionError("subclass missing __call__ should be abstract")


async def test_concrete_hook_is_callable_and_returns_update():
    hook = _OkHook()
    assert isinstance(hook, Hook)
    result = await hook(
        AgentState(messages=[], halted=False),
        _runtime(),
        {"configurable": {"thread_id": "1"}},
    )
    assert result == {"halted": True}


def test_name_defaults_to_class_name():
    assert _OkHook().name == "_OkHook"


def test_name_is_overridable():
    assert _NamedHook().name == "custom-label"
