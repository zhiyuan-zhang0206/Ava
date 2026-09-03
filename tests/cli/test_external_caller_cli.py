"""CLI provenance is opt-in, explicit and never silently downgraded."""

from unittest.mock import Mock

import pytest

from cli.commands import agents


@pytest.fixture
def post(monkeypatch: pytest.MonkeyPatch) -> Mock:
    response = Mock()
    response.status_code = 200
    response.json.return_value = {"status": "enqueued"}
    call = Mock(return_value=response)
    monkeypatch.setattr("shared.http_dial.post", call)
    monkeypatch.setattr("shared.machine.gateway_api_base", lambda: "http://localhost")
    monkeypatch.setattr("shared.machine.gateway_auth_headers", dict)
    monkeypatch.setenv("AVA_CALLER_IDENTITY", '{"kind":"external_agent","subject":"codex"}')
    return call


def test_send_uses_profile_without_repeated_source_flag(post: Mock) -> None:
    agents.cmd_agents_send(42, "hello", None)
    assert post.call_args.kwargs["json"]["source"] == "external_agent:codex"


def test_lifecycle_carries_profile(post: Mock) -> None:
    agents.cmd_agents_restart(42)
    assert post.call_args.kwargs["json"]["source"] == "external_agent:codex"
    agents.cmd_agents_resurrect(42)
    assert post.call_args.kwargs["json"]["resurrected_by"] == "external_agent:codex"
    agents.cmd_agents_kill(42)
    assert post.call_args.kwargs["json"] == {"force": True, "source": "external_agent:codex"}


def test_conflict_fails_before_network(post: Mock) -> None:
    with pytest.raises(ValueError, match="conflicts"):
        agents.cmd_agents_kill(42, source="user")
    post.assert_not_called()


def test_missing_send_identity_is_not_human(post: Mock, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AVA_CALLER_IDENTITY")
    with pytest.raises(ValueError, match="requires --source"):
        agents.cmd_agents_send(42, "hello", None)
    post.assert_not_called()
