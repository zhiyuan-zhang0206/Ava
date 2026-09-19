"""CLI provenance is explicit: the parse layer enforces it; nothing compensates silently."""

import argparse
from unittest.mock import Mock

import pytest

from cli.commands import agents


def _send_args(**overrides: object) -> argparse.Namespace:
    fields: dict[str, object] = {
        "agent_id": 42,
        "content": "hello",
        "source": None,
        "tail_file": None,
    }
    fields.update(overrides)
    return argparse.Namespace(**fields)


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


# -- send: the explicit --source is the only provenance; the profile is not consulted --


def test_send_carries_explicit_source(post: Mock, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AVA_CALLER_IDENTITY")
    agents.cmd_agents_send(42, "hello", "user")
    assert post.call_args.kwargs["json"]["source"] == "user"


def test_send_honours_explicit_source_under_profile(post: Mock) -> None:
    # User ruling 2026-09-20: the send path takes the explicit parameter and
    # neither falls back to nor is vetoed by the inherited AVA_CALLER_IDENTITY.
    agents.cmd_agents_send(42, "hello", "user")
    assert post.call_args.kwargs["json"]["source"] == "user"


def test_missing_send_source_is_not_compensated(post: Mock) -> None:
    # The tightened contract: no --source refuses, even under a profile.
    with pytest.raises(ValueError, match="requires --source"):
        agents.cmd_agents_send(42, "hello", None)
    post.assert_not_called()


# -- the parse layer enforces the requirement before any command code runs --


def test_send_requires_source_at_parse_time(capsys: pytest.CaptureFixture[str]) -> None:
    from cli.parsers import build_parser

    with pytest.raises(SystemExit) as exit_info:
        build_parser().parse_args(["agents", "send", "1", "hi"])
    assert exit_info.value.code == 2
    assert "--source" in capsys.readouterr().err


def test_send_rejects_unknown_source_at_parse_time(capsys: pytest.CaptureFixture[str]) -> None:
    from cli.parsers import build_parser

    with pytest.raises(SystemExit) as exit_info:
        build_parser().parse_args(["agents", "send", "1", "hi", "--source", "bogus"])
    assert exit_info.value.code == 2
    assert "Unrecognized inbound source" in capsys.readouterr().err


# -- the CLI boundary still reports a provenance failure without a traceback --


def test_send_without_provenance_exits_cleanly(
    post: Mock, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from cli.parsers.agents import _h_agents_send

    monkeypatch.delenv("AVA_CALLER_IDENTITY")
    assert _h_agents_send(_send_args()) == 2
    err = capsys.readouterr().err
    assert "requires --source" in err
    assert "--source user" in err
    post.assert_not_called()


def test_send_invalid_source_exits_cleanly(
    post: Mock, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from cli.parsers.agents import _h_agents_send

    monkeypatch.delenv("AVA_CALLER_IDENTITY")
    assert _h_agents_send(_send_args(source="bogus")) == 2
    assert "Unrecognized inbound source" in capsys.readouterr().err
    post.assert_not_called()


# -- lifecycle verbs keep the profile opt-in until their #4092 audit lands --


def test_lifecycle_carries_profile(post: Mock) -> None:
    agents.cmd_agents_restart(42)
    assert post.call_args.kwargs["json"]["source"] == "external_agent:codex"
    agents.cmd_agents_resurrect(42)
    assert post.call_args.kwargs["json"]["resurrected_by"] == "external_agent:codex"
    agents.cmd_agents_kill(42)
    assert post.call_args.kwargs["json"] == {"force": True, "source": "external_agent:codex"}


def test_lifecycle_conflict_fails_before_network(post: Mock) -> None:
    with pytest.raises(ValueError, match="conflicts"):
        agents.cmd_agents_kill(42, source="user")
    post.assert_not_called()


def test_lifecycle_conflict_exits_cleanly(post: Mock, capsys: pytest.CaptureFixture[str]) -> None:
    from cli.parsers.agents import _h_agents_kill

    args = argparse.Namespace(agent_id=42, source="user", final=False)
    assert _h_agents_kill(args) == 2
    assert "conflicts" in capsys.readouterr().err
    post.assert_not_called()
