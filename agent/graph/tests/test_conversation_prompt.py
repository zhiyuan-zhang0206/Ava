"""Human response policy stays active independently of optional narration."""

import pytest

from agent.graph.prompt.conversation import user_reply_section
from agent.graph.prompt.system_prompt import build_system_prompt
from base.config import settings
from base.host.env.agent_slices import AgentSlices
from base.packages.plugins.extensions import EMPTY


@pytest.mark.parametrize("style", ["off", "oriented", "concise", "silent"])
def test_initial_human_response_is_present_without_plugins(
    monkeypatch: pytest.MonkeyPatch, style: str
) -> None:
    monkeypatch.setattr(settings.agent, "agent_communication_style", style)
    slices = AgentSlices.resolve()

    prompt = build_system_prompt(EMPTY, slices)

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
