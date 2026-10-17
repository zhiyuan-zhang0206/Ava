import subprocess

import pytest

import base.deploy.git.github_pr as gp
from base.host.system.hostport import Host
from base.host.system.tests.fake_commands import FakeCommands, Reply, fake_host


def test_slug_parses_ssh_form(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gp, "memory_remote", lambda: "git@github.com:example-org/MeivaMemory.git")
    assert gp.memory_repo_slug() == "example-org/MeivaMemory"


def test_slug_parses_https_form_with_git_suffix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        gp, "memory_remote", lambda: "https://github.com/example-org/MeivaMemory.git"
    )
    assert gp.memory_repo_slug() == "example-org/MeivaMemory"


def test_slug_parses_https_form_without_git_suffix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gp, "memory_remote", lambda: "https://github.com/owner/name")
    assert gp.memory_repo_slug() == "owner/name"


def test_slug_raises_on_non_github_remote(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gp, "memory_remote", lambda: "git@gitlab.com:owner/name.git")
    with pytest.raises(ValueError, match="not a GitHub remote"):
        gp.memory_repo_slug()


def _host(commands: FakeCommands, *, gh: str | None = "/usr/bin/gh") -> Host:
    return fake_host(commands, programs={"gh": gh} if gh else {})


def _patch_slug(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gp, "memory_remote", lambda: "git@github.com:owner/MeivaMemory.git")


def _authenticated(permission: Reply) -> FakeCommands:
    commands = FakeCommands()
    commands.on(["gh", "auth", "status"], Reply())
    commands.on(["gh", "repo", "view"], permission)
    return commands


def test_blocker_none_when_all_checks_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_slug(monkeypatch)
    commands = _authenticated(Reply(stdout="WRITE\n"))
    assert gp.github_pr_blocker(host=_host(commands)) is None
    assert commands.argvs == [
        ("gh", "auth", "status"),
        (
            "gh",
            "repo",
            "view",
            "owner/MeivaMemory",
            "--json",
            "viewerPermission",
            "-q",
            ".viewerPermission",
        ),
    ]
    assert [call.timeout for call in commands.calls] == [20, 20]


def test_blocker_gh_missing() -> None:
    commands = FakeCommands()
    assert gp.github_pr_blocker(host=_host(commands, gh=None)) == "gh CLI not installed"
    assert commands.calls == []


def test_blocker_not_authenticated(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_slug(monkeypatch)
    commands = FakeCommands()
    commands.on("gh", Reply(returncode=1))
    reason = gp.github_pr_blocker(host=_host(commands))
    assert reason is not None
    assert "not authenticated" in reason
    assert commands.argvs == [("gh", "auth", "status")]


def test_blocker_read_only_permission(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_slug(monkeypatch)
    commands = _authenticated(Reply(stdout="READ\n"))
    assert (
        gp.github_pr_blocker(host=_host(commands))
        == "gh account lacks write access to owner/MeivaMemory (viewerPermission='READ')"
    )


def test_blocker_permission_read_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_slug(monkeypatch)
    commands = _authenticated(Reply(returncode=1))
    assert (
        gp.github_pr_blocker(host=_host(commands))
        == "could not read permission on owner/MeivaMemory via gh"
    )


def test_blocker_auth_timeout_is_reason_not_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_slug(monkeypatch)
    commands = FakeCommands()
    commands.on("gh", subprocess.TimeoutExpired(cmd="gh", timeout=20))
    reason = gp.github_pr_blocker(host=_host(commands))
    assert reason is not None
    assert "timed out" in reason
