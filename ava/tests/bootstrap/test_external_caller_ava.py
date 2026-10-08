"""The SDK's actor resolution: an explicit external profile beats an inherited agent id.

A hosted turn context stays authoritative over both."""

import pytest

import ava
from tests.fixtures.pin_agent import exec_context, pin_agent, pin_no_identity


def test_sdk_external_profile_overrides_inherited_agent_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ava.sdk_surface import agent_identity

    pin_agent(405)
    monkeypatch.setenv("AVA_CALLER_IDENTITY", '{"kind":"external_agent","subject":"codex"}')
    assert agent_identity.require_actor() == "external_agent:codex"
    assert agent_identity.default_actor() == "external_agent:codex"


def test_explicit_host_context_remains_authoritative(monkeypatch: pytest.MonkeyPatch) -> None:
    from ava.sdk_surface import agent_identity

    pin_no_identity()
    monkeypatch.delenv("AVA_AGENT_ID", raising=False)
    monkeypatch.setenv("AVA_CALLER_IDENTITY", '{"kind":"external_agent","subject":"codex"}')
    assert agent_identity.require_actor(exec_context(405)) == "agent:405"
    assert agent_identity.require_actor(exec_context(406)) == "agent:406"
    assert agent_identity.require_actor() == "external_agent:codex"
    assert getattr(ava, "context", None) is None
