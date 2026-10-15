from typing import Any

import pytest

from agent.graph.prompt.system_prompt import build_system_prompt
from base.config import settings
from base.config.tests.configuration import defaults
from base.host.env.agent_slices import AgentSlices
from base.packages.plugin_providers import build_model_catalog
from base.packages.plugins.extensions import EMPTY

_SENTENCE = "Several tool calls in one response run one at a time, in the order given"


def _prompt(**changes: Any) -> str:
    return build_system_prompt(
        EMPTY,
        AgentSlices.resolve(default_reader=defaults(**changes)),
        agent_id=1,
        catalog=build_model_catalog(),
    )


def test_sequential_note_defaults_to_off() -> None:
    assert settings.agent.prompt_sequential_tool_calls_enabled is False
    prompt = _prompt()
    assert _SENTENCE not in prompt
    assert "tool calls.\n\nBefore using any `ava.*` function" in prompt


@pytest.mark.parametrize("sdk_overview", [True, False])
@pytest.mark.parametrize("enabled", [True, False])
def test_sequential_note_gating(enabled: bool, sdk_overview: bool) -> None:

    prompt = _prompt(
        agent__prompt_sequential_tool_calls_enabled=enabled,
        agent__prompt_sdk_overview_enabled=sdk_overview,
    )

    assert (_SENTENCE in prompt) is enabled
    assert "{_SEQUENTIAL_TOOL_CALLS}" not in prompt
    if enabled:
        assert "an error in one\ndoes not stop the later ones" in prompt
