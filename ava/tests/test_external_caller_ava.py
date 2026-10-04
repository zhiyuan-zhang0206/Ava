"""The SDK's actor resolution: an explicit external profile beats an inherited agent id.

A hosted turn context stays authoritative over both."""

import pytest

from tests.fixtures.pin_agent import pin_agent


def test_sdk_external_profile_overrides_inherited_agent_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ava import agent_identity

    monkeypatch.setattr(agent_identity, "current_turn_agent_id", lambda: None)
    pin_agent(405)
    monkeypatch.setenv("AVA_CALLER_IDENTITY", '{"kind":"external_agent","subject":"codex"}')
    assert agent_identity.require_actor() == "external_agent:codex"
    assert agent_identity.default_actor() == "external_agent:codex"


def test_actual_hosted_turn_context_remains_authoritative(monkeypatch: pytest.MonkeyPatch) -> None:
    from ava import agent_identity

    monkeypatch.setattr(agent_identity, "current_turn_agent_id", lambda: 405)
    monkeypatch.setenv("AVA_CALLER_IDENTITY", '{"kind":"external_agent","subject":"codex"}')
    assert agent_identity.require_actor() == "agent:405"
