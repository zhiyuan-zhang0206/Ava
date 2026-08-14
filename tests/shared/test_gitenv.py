"""shared.gitenv — the non-interactive environment every Ava git call runs under."""

from __future__ import annotations

import pytest

from shared.gitenv import git_env


def test_prompts_are_off_and_ssh_is_bounded() -> None:
    """Both knobs are present: a missing credential errors instead of reading a
    terminal, and ssh neither asks nor dials forever."""
    env = git_env()
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_SSH_COMMAND"] == "ssh -o BatchMode=yes -o ConnectTimeout=10"


def test_overrides_an_inherited_ssh_command(monkeypatch: pytest.MonkeyPatch) -> None:
    """An inherited `GIT_SSH_COMMAND` does not win. This is the posture Ava
    requires of its own git calls, not a default — and `GIT_SSH_COMMAND` also
    outranks the `core.sshCommand` a debug script left in the Windows box's global
    gitconfig, which is why the neutralisation happens here."""
    monkeypatch.setenv("GIT_SSH_COMMAND", '"C:/Windows/System32/OpenSSH/ssh.exe"')
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "1")
    env = git_env()
    assert env["GIT_SSH_COMMAND"].startswith("ssh -o BatchMode=yes")
    assert env["GIT_TERMINAL_PROMPT"] == "0"


def test_carries_the_rest_of_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """A git subprocess still needs PATH, HOME, SSH_AUTH_SOCK — the helper adds
    to the environment rather than replacing it."""
    monkeypatch.setenv("AVA_GITENV_CANARY", "kept")
    assert git_env()["AVA_GITENV_CANARY"] == "kept"
