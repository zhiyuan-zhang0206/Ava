"""Core communication policy survives optional styles and output-shape toggles."""

from typing import cast

import pytest

from agent.graph.prompt.conversation import user_reply_section
from agent.graph.prompt.system_prompt import build_system_prompt
from base.config import settings
from base.host.env.agent_slices import AgentSlices
from base.packages.plugin_providers import build_model_catalog
from base.packages.plugins.extensions import EMPTY


@pytest.mark.parametrize("style", ["off", "oriented", "concise", "silent"])
def test_initial_human_response_is_present_without_plugins(
    monkeypatch: pytest.MonkeyPatch, style: str
) -> None:
    monkeypatch.setattr(settings.agent, "agent_communication_style", style)
    slices = AgentSlices.resolve(
        default_reader=lambda domain, field: getattr(getattr(settings, domain), field)
    )

    prompt = build_system_prompt(EMPTY, slices, agent_id=1, catalog=build_model_catalog())

    assert prompt.count(user_reply_section(slices)) == 1
    assert "before the first tool call" in prompt
    assert "If you can answer directly, give the answer" in prompt
    assert "deliver the result in that conversation" in prompt
    assert "Peer messages, watcher events, scheduled wakes and system notes" in prompt
    assert "do not require courtesy replies" in prompt
    if style == "silent":
        assert "Work without narrating after the required initial reply" in prompt
        assert "Silence here costs the user nothing" not in prompt
    if style != "off":
        assert "A reply already delivered in the live dialog does not need a duplicate" in prompt


@pytest.mark.parametrize("style", ["off", "oriented", "concise", "silent"])
def test_reply_routing_and_handoff_policy_survive_every_style(
    monkeypatch: pytest.MonkeyPatch, style: str
) -> None:
    monkeypatch.setattr(settings.agent, "agent_communication_style", style)
    slices = AgentSlices.resolve(
        default_reader=lambda domain, field: getattr(getattr(settings, domain), field)
    )
    prompt = build_system_prompt(EMPTY, slices, agent_id=1, catalog=build_model_catalog())

    assert prompt.count("Reply in ordinary assistant text in this conversation") == 1
    assert "Text alongside a tool call also reaches the user" in prompt
    assert "no separate SDK call is needed" in prompt
    assert "when resuming unfinished human requests from a handoff" in prompt
    assert "further investigation should address a concrete remaining question" in prompt
    assert "rather than inventing additional ones" in prompt
    assert "ava.ui.notify" not in user_reply_section(slices)


@pytest.fixture(
    params=[
        (style, shape)
        for style in ["off", "oriented", "concise", "silent"]
        for shape in [False, True]
    ]
)
def communication_prompt(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> tuple[str, str]:
    """Build every optional-guidance combination without collaboration plugins."""
    style, output_shape = cast(tuple[str, bool], request.param)
    monkeypatch.setattr(settings.agent, "agent_communication_style", style)
    monkeypatch.setattr(settings.agent, "prompt_output_conciseness_enabled", output_shape)
    slices = AgentSlices.resolve(
        default_reader=lambda domain, field: getattr(getattr(settings, domain), field)
    )

    return style, build_system_prompt(EMPTY, slices, agent_id=1, catalog=build_model_catalog())


def test_language_policy_survives_optional_guidance(communication_prompt: tuple[str, str]) -> None:
    """Standing language preferences cannot disappear with optional guidance."""
    _, prompt = communication_prompt

    assert prompt.count("## Communication language") == 1
    assert "Follow the current request first, then standing user preferences" in prompt
    assert "injected memory or project instructions" in prompt
    assert "English prompts, code, logs and peer messages do not reset that preference" in prompt
    assert "Preserve code, identifiers, quotations and requested artifacts" in prompt


def test_user_facing_text_policy_survives_every_style(
    communication_prompt: tuple[str, str],
) -> None:
    """Only explicit live pairing adds checkpoints; text always serves the user."""
    style, prompt = communication_prompt

    assert prompt.count("## Meaningful progress") == 1
    assert "Treat ordinary assistant text as substantive communication with the user" in prompt
    assert "Every message must be independently useful to the user" in prompt
    assert "without watching tool execution or reconstructing your internal steps" in prompt
    assert "Make it self-contained: state what changed, why it matters" in prompt
    assert ("This agent is configured for live pairing" in prompt) == (style == "oriented")
    assert "every message must remain self-contained and useful to the user" in prompt
    assert "Do not assume continuous attention to the session" in prompt
    assert "after an hour away" not in prompt


def test_milestone_policy_preserves_results_and_questions(
    communication_prompt: tuple[str, str],
) -> None:
    """The default filters internal steps without suppressing actual deliverables."""
    _, prompt = communication_prompt

    assert "meaningful milestones for the user: a usable result" in prompt
    assert "Completing an internal step or ending a turn is not itself a milestone" in prompt
    assert "Do not narrate routine tool calls, retries, file reads, internal bookkeeping" in prompt
    assert "There is no fixed reporting cadence" in prompt
    assert "without a tool-call preamble or trailing introduction" in prompt
    assert "Direct answers, necessary questions" in prompt
    assert "omitting evidence needed to use the result" in prompt
    assert "do not require courtesy replies or automatic closing summaries" in prompt
    assert "emit alongside each action" not in prompt
