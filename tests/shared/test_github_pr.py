"""shared.github_pr: derive the memory repo slug + gate on gh PR capability.

`memory_repo_slug()` reads the remote URL through `shared.memory_repo.memory_remote`,
which `github_pr` re-imports into its own namespace; tests patch
`gp.memory_remote`. `github_pr_blocker()` shells out to `gh` via `shutil.which`
+ `subprocess.run`; tests patch both so no network / gh install is required.
"""

import subprocess
from dataclasses import dataclass

import pytest

import shared.github_pr as gp

# ── memory_repo_slug ──────────────────────────────────────────────────────


def test_slug_parses_ssh_form(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gp, "memory_remote", lambda: "git@github.com:example-org/AvaMemory.git")
    assert gp.memory_repo_slug() == "example-org/AvaMemory"


def test_slug_parses_https_form_with_git_suffix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gp, "memory_remote", lambda: "https://github.com/example-org/AvaMemory.git")
    assert gp.memory_repo_slug() == "example-org/AvaMemory"


def test_slug_parses_https_form_without_git_suffix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gp, "memory_remote", lambda: "https://github.com/owner/name")
    assert gp.memory_repo_slug() == "owner/name"


def test_slug_raises_on_non_github_remote(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gp, "memory_remote", lambda: "git@gitlab.com:owner/name.git")
    with pytest.raises(ValueError, match="not a GitHub remote"):
        gp.memory_repo_slug()


# ── github_pr_blocker ─────────────────────────────────────────────────────


@dataclass
class _FakeProc:
    returncode: int
    stdout: str = ""


def _patch_slug(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gp, "memory_remote", lambda: "git@github.com:owner/AvaMemory.git")


def test_blocker_none_when_all_checks_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_slug(monkeypatch)
    monkeypatch.setattr(gp.shutil, "which", lambda _: "/usr/bin/gh")  # pyright: ignore[reportUnknownArgumentType]

    def fake_run(args, **_):
        if args[:3] == ["gh", "auth", "status"]:
            return _FakeProc(returncode=0)
        return _FakeProc(returncode=0, stdout="WRITE\n")

    monkeypatch.setattr(gp.subprocess, "run", fake_run)  # pyright: ignore[reportUnknownArgumentType]
    assert gp.github_pr_blocker() is None


def test_blocker_gh_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gp.shutil, "which", lambda _: None)  # pyright: ignore[reportUnknownArgumentType]
    assert gp.github_pr_blocker() == "gh CLI not installed"


def test_blocker_not_authenticated(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_slug(monkeypatch)
    monkeypatch.setattr(gp.shutil, "which", lambda _: "/usr/bin/gh")  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(gp.subprocess, "run", lambda *_a, **_k: _FakeProc(returncode=1))  # pyright: ignore[reportUnknownArgumentType]
    reason = gp.github_pr_blocker()
    assert reason is not None
    assert "not authenticated" in reason


def test_blocker_read_only_permission(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_slug(monkeypatch)
    monkeypatch.setattr(gp.shutil, "which", lambda _: "/usr/bin/gh")  # pyright: ignore[reportUnknownArgumentType]

    def fake_run(args, **_):
        if args[:3] == ["gh", "auth", "status"]:
            return _FakeProc(returncode=0)
        return _FakeProc(returncode=0, stdout="READ\n")

    monkeypatch.setattr(gp.subprocess, "run", fake_run)  # pyright: ignore[reportUnknownArgumentType]
    assert (
        gp.github_pr_blocker()
        == "gh account lacks write access to owner/AvaMemory (viewerPermission='READ')"
    )


def test_blocker_permission_read_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_slug(monkeypatch)
    monkeypatch.setattr(gp.shutil, "which", lambda _: "/usr/bin/gh")  # pyright: ignore[reportUnknownArgumentType]

    def fake_run(args, **_):
        if args[:3] == ["gh", "auth", "status"]:
            return _FakeProc(returncode=0)
        return _FakeProc(returncode=1, stdout="")

    monkeypatch.setattr(gp.subprocess, "run", fake_run)  # pyright: ignore[reportUnknownArgumentType]
    assert gp.github_pr_blocker() == "could not read permission on owner/AvaMemory via gh"


def test_blocker_auth_timeout_is_reason_not_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_slug(monkeypatch)
    monkeypatch.setattr(gp.shutil, "which", lambda _: "/usr/bin/gh")  # pyright: ignore[reportUnknownArgumentType]

    def fake_run(args, **_):
        raise subprocess.TimeoutExpired(cmd=args, timeout=20)  # pyright: ignore[reportUnknownArgumentType]

    monkeypatch.setattr(gp.subprocess, "run", fake_run)  # pyright: ignore[reportUnknownArgumentType]
    reason = gp.github_pr_blocker()
    assert reason is not None
    assert "timed out" in reason
