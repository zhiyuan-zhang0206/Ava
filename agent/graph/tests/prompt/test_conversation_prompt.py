"""Human response policy stays active independently of optional narration."""

import pytest

from agent.graph.prompt.conversation import user_reply_section
from agent.graph.prompt.system_prompt import build_system_prompt
from base.config import settings
from base.host.env.agent_slices import AgentSlices
from base.lm.plugin_providers import build_model_catalog
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
