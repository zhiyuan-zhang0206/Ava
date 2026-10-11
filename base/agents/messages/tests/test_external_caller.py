"""Explicit external profiles cannot impersonate an inherited Ava agent."""

import json

import pytest

from base.agents.messages.external_caller import external_caller, launch_caller_assignment


def test_absence_stays_absent_not_human(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AVA_CALLER_IDENTITY", raising=False)
    assert external_caller() is None


def test_explicit_profile_is_read_as_the_external_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "AVA_CALLER_IDENTITY",
        json.dumps({"kind": "external_agent", "subject": "codex", "instance": "run-42"}),
    )
    monkeypatch.setenv("AVA_AGENT_ID", "405")
    caller = external_caller()
    assert caller is not None
    assert caller.source() == "external_agent:codex:run-42"


@pytest.mark.parametrize(
    "profile",
    [
        "{}",
        "null",
        "bad",
        '{"kind":"human","subject":"user"}',
        '{"kind":"unknown","subject":"cli"}',
        '{"kind":"external_agent","subject":"codex","scopes":["kill"]}',
    ],
)
def test_invalid_profile_fails_without_fallback(
    monkeypatch: pytest.MonkeyPatch, profile: str
) -> None:
    monkeypatch.setenv("AVA_CALLER_IDENTITY", profile)
    with pytest.raises(ValueError):
        external_caller()


@pytest.mark.parametrize("tool", ["codex", "claude_code"])
def test_wrapper_assignment_is_opt_in_and_shell_safe(tool: str) -> None:
    import shlex

    assert launch_caller_assignment(tool, None) == ""
    assignment = shlex.split(launch_caller_assignment(tool, "run-42"))
    assert len(assignment) == 1
    name, value = assignment[0].split("=", 1)
    assert name == "AVA_CALLER_IDENTITY"
    assert json.loads(value) == {"kind": "external_agent", "subject": tool, "instance": "run-42"}
    with pytest.raises(ValueError):
        launch_caller_assignment(tool, "run; rm -rf /")
