"""Explicit external profiles cannot impersonate an inherited Ava agent."""

import json

import pytest

from shared.external_caller import explicit_caller_source, external_caller, launch_caller_assignment


def test_absence_stays_absent_not_human(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AVA_CALLER_IDENTITY", raising=False)
    assert external_caller() is None
    assert explicit_caller_source() is None


def test_explicit_profile_projects_honest_source(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "AVA_CALLER_IDENTITY",
        json.dumps({"kind": "external_agent", "subject": "codex", "instance": "run-42"}),
    )
    monkeypatch.setenv("AVA_AGENT_ID", "405")
    assert explicit_caller_source() == "external_agent:codex:run-42"


@pytest.mark.parametrize("source", ["user", "agent:405", "system", "external_agent:claude_code"])
def test_profile_cannot_be_overridden_with_other_identity(
    monkeypatch: pytest.MonkeyPatch,
    source: str,
) -> None:
    monkeypatch.setenv("AVA_CALLER_IDENTITY", '{"kind":"external_agent","subject":"codex"}')
    with pytest.raises(ValueError, match="conflicts"):
        explicit_caller_source(source)


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
        explicit_caller_source("user")


def test_sdk_external_profile_overrides_inherited_agent_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ava import _boot

    monkeypatch.setattr(_boot, "current_turn_agent_id", lambda: None)
    monkeypatch.setattr(_boot, "_agent_id", 405)
    monkeypatch.setenv("AVA_CALLER_IDENTITY", '{"kind":"external_agent","subject":"codex"}')
    assert _boot.require_actor() == "external_agent:codex"
    assert _boot.default_actor() == "external_agent:codex"


def test_actual_hosted_turn_context_remains_authoritative(monkeypatch: pytest.MonkeyPatch) -> None:
    from ava import _boot

    monkeypatch.setattr(_boot, "current_turn_agent_id", lambda: 405)
    monkeypatch.setenv("AVA_CALLER_IDENTITY", '{"kind":"external_agent","subject":"codex"}')
    assert _boot.require_actor() == "agent:405"


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
