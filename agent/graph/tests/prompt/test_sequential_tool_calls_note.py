"""The sequential tool-call sentence in the base prompt preamble.

`prompt_sequential_tool_calls_enabled` (AVA_SYSTEM_PROMPT_SEQUENTIAL_TOOL_CALLS)
is off by default and adds one sentence to the preamble when on, both with the
SDK overview and in the bare identity.
"""

import pytest

from agent.graph.prompt.system_prompt import build_system_prompt
from base.config import settings
from base.host.env.agent_slices import AgentSlices
from base.lm.plugin_providers import build_model_catalog
from base.packages.plugins.extensions import EMPTY

_SENTENCE = "Several tool calls in one response run one at a time, in the order given"


def _prompt() -> str:
    return build_system_prompt(
        EMPTY,
        AgentSlices.resolve(
            default_reader=lambda domain, field: getattr(getattr(settings, domain), field)
        ),
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
def test_sequential_note_gating(
    monkeypatch: pytest.MonkeyPatch, enabled: bool, sdk_overview: bool
) -> None:
    monkeypatch.setattr(settings.agent, "prompt_sequential_tool_calls_enabled", enabled)
    monkeypatch.setattr(settings.agent, "prompt_sdk_overview_enabled", sdk_overview)

    prompt = _prompt()

    assert (_SENTENCE in prompt) is enabled
    assert "{_SEQUENTIAL_TOOL_CALLS}" not in prompt
    if enabled:
        assert "an error in one\ndoes not stop the later ones" in prompt
