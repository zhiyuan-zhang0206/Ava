from typing import Any, cast

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.runtime import Runtime

from agent.graph.hooks.compact import (
    auto_compact_will_fire,
    compact_reminder,
    emit_compaction_monitoring,
)
from agent.hooks import Hook, make_hook_runner
from agent.messages import inbound_message
from agent.state import AgentState, CompactState
from base.agents.context import AvaContext
from base.config.tests.configuration import defaults
from base.host.env.agent_slices import AgentSlices
from base.lm.catalog import ModelCatalog
from base.packages.plugin_providers import build_model_catalog
from base.packages.plugins.extensions import EMPTY, ExtensionRegistry, PluginContributions


def _runtime(
    extensions: ExtensionRegistry = EMPTY, *, hard_ceiling: int = 0
) -> Runtime[AvaContext]:
    return Runtime(
        context=AvaContext(
            agent=AgentSlices.resolve(
                {
                    "llm_model": "deepseek-flash",
                    "compact_reminder_fraction": 0.000001,
                    "auto_compact_fraction": 0.4,
                    "auto_compact_ceiling_tokens": hard_ceiling,
                },
                default_reader=defaults(),
            ),
            catalog=build_model_catalog(),
            extensions=extensions,
        )
    )


async def test_contributors_run_once_in_order_and_rearm_in_the_next_window() -> None:
    calls: list[str] = []

    def first(slices: AgentSlices, *, catalog: ModelCatalog) -> str:
        assert slices.brain.llm_model == "deepseek-flash"
        assert catalog.models
        calls.append("first")
        return "Persist the accepted commitments."

    def empty(_slices: AgentSlices, *, catalog: ModelCatalog) -> str:
        calls.append("empty")
        return ""

    def second(_slices: AgentSlices, *, catalog: ModelCatalog) -> str:
        calls.append("second")
        return "Record evidence of observed failures."

    registry = ExtensionRegistry(
        (
            ("fleet", PluginContributions(compact_reminder_sections=(first, empty))),
            ("evolution", PluginContributions(compact_reminder_sections=(second,))),
        )
    )
    state = AgentState(messages=[HumanMessage(content="history " * 200)])
    runtime = _runtime(registry)
    result = await compact_reminder(state, runtime, {})
    assert result is not None
    assert calls == ["first", "empty", "second"]
    [note] = result["messages"]
    assert "to_user.md" in note.content
    assert "[fleet]\nPersist" in note.content
    assert note.content.index("[fleet]") < note.content.index("[evolution]")
    assert note.content.count("[fleet]") == 1
    prepared = state.model_copy(update={"compact": result["compact"]})
    assert await compact_reminder(prepared, runtime, {}) is None
    assert calls == ["first", "empty", "second"]
    next_window = prepared.model_copy(update={"compact": prepared.compact.next_segment()})
    assert await compact_reminder(next_window, runtime, {}) is not None
    assert calls == ["first", "empty", "second"] * 2


async def test_contributors_do_not_run_below_soft_or_above_hard() -> None:
    def unexpected(_slices: AgentSlices, *, catalog: ModelCatalog) -> str:
        pytest.fail("a non-firing reminder called its contributor")

    runtime = _runtime(
        ExtensionRegistry(
            (("probe", PluginContributions(compact_reminder_sections=(unexpected,))),)
        )
    )
    assert await compact_reminder(AgentState(), runtime, {}) is None
    forced = _runtime(runtime.context.plugin_registry(), hard_ceiling=1)
    assert (
        await compact_reminder(
            AgentState(messages=[HumanMessage(content="history " * 200)]), forced, {}
        )
        is None
    )


class _PeerReminder(Hook):
    async def __call__(self, state: Any, runtime: Any, config: Any, /) -> dict[str, Any]:
        return {"messages": [HumanMessage(content="Reply to the peer when needed.")]}


async def test_invalid_contributor_result_does_not_mark_preparation_as_shown() -> None:
    def invalid(_slices: AgentSlices, *, catalog: ModelCatalog) -> str:
        return cast(str, None)

    runtime = _runtime(
        ExtensionRegistry((("broken", PluginContributions(compact_reminder_sections=(invalid,))),))
    )
    state = AgentState(messages=[HumanMessage(content="history " * 200)])
    with pytest.raises(TypeError, match=r"broken\.invalid must return reminder text"):
        await compact_reminder(state, runtime, {})
    assert not state.compact.reminder_shown


@pytest.mark.parametrize("compact_first", [True, False])
async def test_soft_reminder_composes_with_another_message_writer(compact_first: bool) -> None:
    hooks = [(None, compact_reminder), ("peer", _PeerReminder())]
    runner = make_hook_runner("before_llm", "llm", hooks if compact_first else hooks[::-1])
    state = AgentState(
        messages=[
            HumanMessage(content="history " * 200),
            inbound_message(content="progress?", source="agent:7", inbound_id=1, body_start=0),
        ]
    )
    command = await runner(state, _runtime(), {})
    update = cast(dict[str, Any], command.update)
    assert command.goto == "llm"
    assert len(update["messages"]) == 2
    assert update["compact"].reminder_shown
    assert any("ava.self.compact(summary)" in m.content for m in update["messages"])


@pytest.mark.parametrize(
    ("version", "shown", "seen", "expected"),
    [(0, False, 0, False), (0, True, 0, True), (1, True, 0, False), (1, True, 1, True)],
)
def test_completion_attributes_only_the_current_windows_reminder(
    version: int, shown: bool, seen: int, expected: bool, loguru_records: list[dict[str, Any]]
) -> None:
    emit_compaction_monitoring(
        [HumanMessage(content="history")],
        "summary",
        agent_id=1,
        compact_kind="compact_summary",
        compact=CompactState(version=version, reminder_shown=shown, reminder_seen_version=seen),
    )
    events = [
        record
        for record in loguru_records
        if record["extra"].get("event") == "compaction_completed"
    ]
    assert events
    assert events[-1]["extra"]["reminder_shown"] is expected


@pytest.mark.parametrize("kind", ["compact_summary", "compact_request"])
async def test_committed_summary_gets_an_ordinary_reply_before_preparation_rearms(
    kind: str,
) -> None:
    runtime = _runtime(hard_ceiling=1)
    state = AgentState(
        messages=[
            SystemMessage(content="standing head"),
            HumanMessage(content="summary " * 200, additional_kwargs={"ava_msg_type": kind}),
            HumanMessage(content="new inbound"),
        ]
    )
    agent = runtime.context.require_agent()
    catalog = runtime.context.require_catalog()
    assert not auto_compact_will_fire(state, agent, catalog=catalog)
    assert await compact_reminder(state, runtime, {}) is None
    replied = state.model_copy(update={"messages": [*state.messages, AIMessage(content="reply")]})
    assert auto_compact_will_fire(replied, agent, catalog=catalog)
    soft = _runtime()
    assert await compact_reminder(state, soft, {}) is None
    assert await compact_reminder(replied, soft, {}) is not None
