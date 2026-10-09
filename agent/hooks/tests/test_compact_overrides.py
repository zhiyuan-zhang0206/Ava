"""The compaction gates read the agent's own thresholds.

`auto_compact_will_fire` and the wind-down reminder derive their token thresholds from the
agent's model and its `overrides` slice (the agent's pins of `auto_compact_fraction`,
`auto_compact_ceiling_tokens`, `compact_reminder_fraction`). The conversation here is a few
thousand tokens against a window of hundreds of thousands, so the model's own thresholds sit far
above it and only a pinned one moves a gate.
"""

from __future__ import annotations

from typing import Any

import pytest
from langchain_core.messages import AnyMessage, HumanMessage, SystemMessage

from agent.hooks.compact import _compact_reminder_update, auto_compact_will_fire
from agent.state import AgentState, CompactState
from base.host.env.agent_slices import AgentSlices
from base.lm.plugin_providers import build_model_catalog

build_model_catalog()

_MODEL = "deepseek-flash"
_TINY_FRACTION = 1e-6  # about one token of the window


def _state() -> AgentState:
    messages: list[AnyMessage] = [
        SystemMessage(content="<sys>"),
        *(HumanMessage(content="x" * 1000, id=f"h{i}") for i in range(5)),
    ]
    return AgentState(messages=messages)


def _agent(**pins: float) -> AgentSlices:
    return AgentSlices.resolve({"llm_model": _MODEL, **pins})


def test_an_unpinned_agent_is_below_its_models_thresholds() -> None:
    assert not auto_compact_will_fire(_state(), _agent(), catalog=build_model_catalog())
    assert _compact_reminder_update(_state(), _agent(), catalog=build_model_catalog()) is None


@pytest.mark.parametrize(
    "pin", [{"auto_compact_fraction": _TINY_FRACTION}, {"auto_compact_ceiling_tokens": 1}]
)
def test_an_agent_threshold_that_sits_below_the_conversation_forces_compaction(
    pin: dict[str, float],
) -> None:
    assert auto_compact_will_fire(_state(), _agent(**pin), catalog=build_model_catalog())


def test_a_raised_force_threshold_is_what_keeps_a_long_conversation_going() -> None:
    state = _state()
    tiny = _agent(auto_compact_fraction=_TINY_FRACTION)
    assert auto_compact_will_fire(state, tiny, catalog=build_model_catalog())
    # The same agent with the force threshold lifted back above the conversation.
    assert not auto_compact_will_fire(
        state, _agent(auto_compact_fraction=0.9), catalog=build_model_catalog()
    )


def test_an_agent_reminder_threshold_below_the_conversation_injects_the_reminder() -> None:
    update: dict[str, Any] | None = _compact_reminder_update(
        _state(), _agent(compact_reminder_fraction=_TINY_FRACTION), catalog=build_model_catalog()
    )
    assert update is not None
    compact: CompactState = update["compact"]
    assert compact.reminder_shown
