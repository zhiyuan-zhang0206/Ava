from unittest.mock import MagicMock

from langchain_core.runnables import RunnableConfig
from langgraph.runtime import Runtime

from agent.hooks import Hook
from agent.hooks.tests.fakes import make_fake_ops_pool
from agent.state import AgentState
from base.agents.context import MeivaContext
from base.clock import Clock
from base.config.tests.configuration import defaults
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.host.env.agent_slices import AgentSlices
from base.packages.plugin_providers import build_model_catalog


class _OkHook(Hook):
    async def __call__(
        self,
        state: AgentState,
        runtime: Runtime[MeivaContext],
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
        runtime: Runtime[MeivaContext],
        config: RunnableConfig,
        /,
    ) -> dict | None:
        return None


def _runtime(database_gate: ProcessDbGate) -> Runtime[MeivaContext]:
    ctx = MeivaContext(
        ops_pool=make_fake_ops_pool(),
        llm=MagicMock(),
        event_publisher=MagicMock(),
        agent=AgentSlices.resolve(default_reader=defaults()),
        db=Database.from_settings(gate=database_gate),
        bus=EventBus.from_settings(),
        catalog=build_model_catalog(),
        clock_factory=Clock.from_settings,
    )
    return Runtime(context=ctx)


def test_base_hook_cannot_be_instantiated():
    cls: type = Hook
    try:
        cls()
    except TypeError:
        return
    raise AssertionError("instantiating abstract Hook should raise TypeError")


def test_subclass_without_call_cannot_be_instantiated():
    class _Incomplete(Hook):
        pass

    cls: type = _Incomplete
    try:
        cls()
    except TypeError:
        return
    raise AssertionError("subclass missing __call__ should be abstract")


async def test_concrete_hook_is_callable_and_returns_update(database_gate: ProcessDbGate):
    hook = _OkHook()
    assert isinstance(hook, Hook)
    result = await hook(
        AgentState(messages=[], halted=False),
        _runtime(database_gate=database_gate),
        {"configurable": {"thread_id": "1"}},
    )
    assert result == {"halted": True}


def test_name_defaults_to_class_name():
    assert _OkHook().name == "_OkHook"


def test_name_is_overridable():
    assert _NamedHook().name == "custom-label"
