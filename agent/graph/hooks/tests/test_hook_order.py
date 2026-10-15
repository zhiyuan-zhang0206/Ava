from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.runtime import Runtime
from pydantic import BaseModel

from agent.graph.hooks.framework import framework_hooks, hooks_at
from agent.hooks import Hook, make_hook_runner
from agent.hooks.repair import repair_dangling_tool_pairing, unpaired_tool_calls
from agent.state import BaseAgentState, PluginStateHandle, build_agent_state, guarded_add_messages
from base.agents.context import AvaContext
from base.config.tests.configuration import defaults
from base.host.env.agent_slices import AgentSlices
from base.packages.plugins.extensions import ExtensionRegistry, HookPoint, PluginContributions


class NoticeState(BaseModel):
    noticed: bool = False


class NoticeHook(Hook):
    def __init__(self, plugin: str) -> None:
        self.handle = PluginStateHandle(NoticeState, plugin)
        self.observed: list[tuple[tuple[str | None, ...], int, bool]] = []

    async def __call__(
        self,
        state: BaseAgentState,
        runtime: Runtime[AvaContext],
        config: RunnableConfig,
        /,
    ) -> dict[str, Any] | None:
        noticed = self.handle.view(state).noticed
        self.observed.append(
            (
                tuple(message.id for message in state.messages),
                len(unpaired_tool_calls(state.messages)),
                noticed,
            )
        )
        if noticed:
            return None
        return {
            "messages": [HumanMessage(content="New skill is available", id="plugin-note")],
            **self.handle.delta({"noticed": True}),
        }


def assert_repaired_notice(committed: BaseAgentState, notice: NoticeHook) -> None:
    assert any(message.id == "plugin-note" for message in committed.messages)
    assert notice.handle.view(committed).noticed is True
    assert unpaired_tool_calls(committed.messages) == []
    assert [
        message.tool_call_id for message in committed.messages if isinstance(message, ToolMessage)
    ] == ["call-1"]


async def test_repair_preserves_the_plugin_note_and_state_across_turns() -> None:
    notice = NoticeHook("notice")
    extensions = ExtensionRegistry(
        (("notice", PluginContributions(state=(NoticeState,), before_llm=(notice,))),)
    )
    state = build_agent_state(extensions)(
        messages=[
            HumanMessage(content="Use the tool", id="user-input"),
            AIMessage(
                content="",
                id="dangling-call",
                tool_calls=[{"name": "execute_code", "args": {"code": "pass"}, "id": "call-1"}],
            ),
        ]
    )
    before = state.model_dump()
    hooks = [
        (plugin, hook)
        for plugin, hook in hooks_at("before_llm", extensions)
        if hook is notice or hook is repair_dangling_tool_pairing
    ]
    run = make_hook_runner("before_llm", "llm", hooks)
    runtime = Runtime(context=AvaContext(agent=AgentSlices.resolve(default_reader=defaults())))
    config: RunnableConfig = {"configurable": {"thread_id": "1"}}

    command = await run(state, runtime, config)
    update = command.update
    assert isinstance(update, dict)
    messages = guarded_add_messages(state.messages, update["messages"])
    committed = state.model_copy(update={**update, "messages": messages})

    assert_repaired_notice(committed, notice)
    assert command.goto == "llm"
    assert state.model_dump() == before
    assert notice.observed == [(("user-input", "dangling-call"), 1, False)]

    second = await run(committed, runtime, config)
    assert second.update == {}
    assert second.goto == "llm"
    assert notice.handle.view(committed).noticed is True
    assert sum(message.id == "plugin-note" for message in committed.messages) == 1
    assert notice.observed[-1] == (tuple(message.id for message in committed.messages), 0, True)


@pytest.mark.parametrize("point", ["after_init", "before_llm", "before_exec", "after_exec"])
def test_only_repair_precedes_plugins_and_every_other_order_is_preserved(point: HookPoint) -> None:
    first, second, third = (NoticeHook(name) for name in ("first", "second", "third"))
    declarations = dict.fromkeys(
        ("after_init", "before_llm", "before_exec", "after_exec"), (first, second)
    )
    last = dict.fromkeys(("after_init", "before_llm", "before_exec", "after_exec"), (third,))
    extensions = ExtensionRegistry(
        (("alpha", PluginContributions(**declarations)), ("beta", PluginContributions(**last)))
    )
    hooks = hooks_at(point, extensions)
    plugins = [("alpha", first), ("alpha", second), ("beta", third)]
    framework = framework_hooks()[point]
    assert [(owner, hook) for owner, hook in hooks if owner is not None] == plugins
    assert [type(hook) for owner, hook in hooks if owner is None] == [
        type(hook) for hook in framework
    ]
    if point == "before_llm":
        assert hooks[0] == (None, repair_dangling_tool_pairing)
        assert sum(hook is repair_dangling_tool_pairing for _, hook in hooks) == 1
        assert hooks[1:4] == plugins
        assert all(owner is None for owner, _ in hooks[4:])
    else:
        assert hooks[:3] == plugins
        assert all(owner is None for owner, _ in hooks[3:])
