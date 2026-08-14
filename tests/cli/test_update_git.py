"""Real-git tests for the SHA-pinned rollout helpers (cluster-update-hardening #5).

`git_checkout_sha` must land every node on the *exact* target commit from any
starting branch or dirty tree — the moving-tip + feature-branch-stuck failures of
the 2026-06-01 collision — discarding (and logging) unpushed local work, since a
prod source tracks the cluster's commit and is not a dev workspace. These run
against a real throwaway origin+clone (the force-checkout semantics are the point;
mocking git would test nothing)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from cli.commands import _update_git as g


def _git(repo: Path, *args: str) -> str:
    # test-controlled git argv (no untrusted input); list-form, not shell
    return subprocess.run(  # noqa: S603
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def cloned_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, str]:
    """A bare origin + a working clone with one commit on main pushed; point the
    helpers' repo root at the clone. Returns (clone_path, origin_main_sha)."""
    origin = tmp_path / "origin.git"
    subprocess.run(  # noqa: S603 — test-controlled git argv
        ["git", "init", "--bare", "-b", "main", str(origin)], check=True, capture_output=True
    )
    clone = tmp_path / "clone"
    subprocess.run(  # noqa: S603 — test-controlled git argv
        ["git", "clone", str(origin), str(clone)], check=True, capture_output=True
    )
    _git(clone, "config", "user.email", "t@example.com")
    _git(clone, "config", "user.name", "tester")
    (clone / "f.txt").write_text("c1")
    _git(clone, "add", "-A")
    _git(clone, "commit", "-m", "c1")
    _git(clone, "push", "origin", "main")
    main_sha = _git(clone, "rev-parse", "HEAD")
    monkeypatch.setattr(g, "_REPO_ROOT_FOR_GIT", clone)
    return clone, main_sha


def test_git_checkout_sha_force_aligns_from_feature_branch(
    cloned_repo: tuple[Path, str], capsys: pytest.CaptureFixture[str]
) -> None:
    """A node left on a feature branch with an unpushed commit (the test-host case) is
    forced back onto main at the target; the local commit is discarded + logged."""
    clone, main_sha = cloned_repo
    _git(clone, "checkout", "-b", "feat/x")
    (clone / "f.txt").write_text("c2")
    _git(clone, "commit", "-am", "c2")
    feat_sha = _git(clone, "rev-parse", "HEAD")
    assert _git(clone, "symbolic-ref", "--short", "HEAD") == "feat/x"

    from_sha = g.git_checkout_sha(main_sha)

    assert from_sha == feat_sha  # returns the prior HEAD
    assert _git(clone, "rev-parse", "HEAD") == main_sha  # landed exactly on target
    assert _git(clone, "symbolic-ref", "--short", "HEAD") == "main"  # back on main, not detached
    assert (clone / "f.txt").read_text() == "c1"  # feature work gone
    assert "discards 1 unpushed local commit" in capsys.readouterr().err  # option B: logged


def test_git_checkout_sha_discards_dirty_tree(cloned_repo: tuple[Path, str]) -> None:
    """Uncommitted local edits on a prod source are discarded by the force checkout."""
    clone, main_sha = cloned_repo
    (clone / "f.txt").write_text("uncommitted local edit")
    g.git_checkout_sha(main_sha)
    assert (clone / "f.txt").read_text() == "c1"


def test_git_checkout_sha_noop_when_already_on_target(
    cloned_repo: tuple[Path, str], capsys: pytest.CaptureFixture[str]
) -> None:
    """Already on the target sha -> lands there, nothing discarded, no warning."""
    clone, main_sha = cloned_repo
    from_sha = g.git_checkout_sha(main_sha)
    assert from_sha == main_sha
    assert _git(clone, "rev-parse", "HEAD") == main_sha
    assert "discards" not in capsys.readouterr().err


def test_git_resolve_origin_main_returns_tip(cloned_repo: tuple[Path, str]) -> None:
    """Fetches + resolves origin/main to the pinned target sha."""
    _clone, main_sha = cloned_repo
    assert g.git_resolve_origin_main() == main_sha


# ───────────── timeout + network retry (`_git` / `_git_network`) ─────────────


def test_git_runs_bounded_and_non_interactive(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every `_git` call goes through `run_bounded` (the timeout bounds the whole
    process tree, not just Git-for-Windows' launcher stub) and carries the
    non-interactive env, so a missing credential errors instead of blocking on a
    terminal that does not exist under a detached rollout."""
    seen: dict[str, object] = {}

    def _capture(argv, **kwargs):
        seen.update(kwargs, argv=argv)  # pyright: ignore[reportUnknownArgumentType]
        return subprocess.CompletedProcess(argv, 0, "sha", "")  # pyright: ignore[reportUnknownArgumentType]

    monkeypatch.setattr(g, "run_bounded", _capture)  # pyright: ignore[reportUnknownArgumentType]
    assert g._git("rev-parse", "HEAD") == "sha"
    assert seen["argv"] == ["git", "rev-parse", "HEAD"]
    assert seen["timeout"] == g._GIT_LOCAL_TIMEOUT_S
    env = seen["env"]
    assert isinstance(env, dict)
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert "BatchMode=yes" in env["GIT_SSH_COMMAND"]


def test_git_timeout_raises_gitpullfailed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A git process hanging past the timeout has its whole tree killed and is
    surfaced as GitPullFailed naming the timeout — previously it hung the detached
    rollout orchestration forever."""

    def _hang(*_a, **kwargs):
        raise subprocess.TimeoutExpired(cmd=["git"], timeout=kwargs.get("timeout", 0))  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]

    monkeypatch.setattr(g, "run_bounded", _hang)  # pyright: ignore[reportUnknownArgumentType]
    with pytest.raises(g.GitPullFailed, match="timed out after"):
        g._git("rev-parse", "HEAD")


def test_git_network_retries_then_succeeds(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A transient network failure heals: two failing attempts, the third
    returns — each retry leaves a log line (no silent retry loops)."""
    calls = {"n": 0}

    def _flaky(*args: str, timeout_s: float = 0.0) -> str:
        calls["n"] += 1
        if calls["n"] < 3:
            raise g.GitPullFailed("simulated network blip")
        return "ok"

    monkeypatch.setattr(g, "_git", _flaky)
    monkeypatch.setattr(g.time, "sleep", lambda _s: None)  # pyright: ignore[reportUnknownArgumentType]
    assert g._git_network("fetch", "origin") == "ok"
    assert calls["n"] == 3
    err = capsys.readouterr().err
    assert err.count("retrying in") == 2


def test_git_network_exhausts_attempts_and_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A persistent failure exhausts the attempts and fails loud, naming the
    attempt count and the last cause."""

    def _dead(*args: str, timeout_s: float = 0.0) -> str:
        raise g.GitPullFailed("persistent failure")

    monkeypatch.setattr(g, "_git", _dead)
    monkeypatch.setattr(g.time, "sleep", lambda _s: None)  # pyright: ignore[reportUnknownArgumentType]
    with pytest.raises(g.GitPullFailed, match=r"after 3 attempts.*persistent failure"):
        g._git_network("fetch", "origin")


def test_local_git_does_not_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Local commands fail immediately on first error — retries would only
    mask a real problem (conflict / corrupt tree)."""
    calls = {"n": 0}

    def _fail_once(*_a, **_k):
        calls["n"] += 1
        raise subprocess.TimeoutExpired(cmd=["git"], timeout=1.0)

    monkeypatch.setattr(g, "run_bounded", _fail_once)  # pyright: ignore[reportUnknownArgumentType]
    with pytest.raises(g.GitPullFailed):
        g.git_head_sha()
    assert calls["n"] == 1


# ───────────── track mode: `releases` (AVA_TRACK_MODE=releases) ─────────────


def _set_track_mode(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    monkeypatch.setattr(g.settings.general, "track_mode", mode)


def test_git_pull_main_releases_checks_out_latest_tag(
    cloned_repo: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """In releases mode, `git_pull_main` converges to the newest dated release
    tag — a main commit pushed after the tag must NOT move HEAD."""
    clone, _main_sha = cloned_repo
    # cut a release tag on the current main, push it, then advance main past it
    _git(clone, "tag", "-a", "v0.8.0-20260801", "-m", "release", "HEAD")
    _git(clone, "push", "origin", "tag", "v0.8.0-20260801")
    tag_sha = _git(clone, "rev-parse", "v0.8.0-20260801^{commit}")
    (clone / "f.txt").write_text("c2")
    _git(clone, "commit", "-am", "c2")
    _git(clone, "push", "origin", "main")
    _set_track_mode(monkeypatch, "releases")

    result = g.git_pull_main()

    assert result.to_sha == tag_sha  # converged to the tag, not the new main tip
    assert _git(clone, "rev-parse", "HEAD") == tag_sha
    assert _git(clone, "symbolic-ref", "--short", "HEAD") == "main"  # not detached


def test_git_resolve_origin_main_releases_returns_tag_sha(
    cloned_repo: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    clone, _main_sha = cloned_repo
    _git(clone, "tag", "-a", "v0.8.1-202608021200", "-m", "release", "HEAD")
    _git(clone, "push", "origin", "tag", "v0.8.1-202608021200")
    (clone / "f.txt").write_text("c2")
    _git(clone, "commit", "-am", "c2")
    _git(clone, "push", "origin", "main")
    _set_track_mode(monkeypatch, "releases")

    # pin must be the tag commit, not the pushed main tip
    assert g.git_resolve_origin_main() == _git(clone, "rev-parse", "v0.8.1-202608021200^{commit}")


def test_tracking_target_ref_releases_no_tag_raises(
    cloned_repo: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A releases-mode cluster with no release tag fails loudly — falling back
    to main would silently break the pinned-release guarantee."""
    _clone, _main_sha = cloned_repo
    _set_track_mode(monkeypatch, "releases")
    with pytest.raises(g.GitPullFailed, match="no dated release tag"):
        g.tracking_target_ref()


def test_git_pull_main_latest_unchanged(
    cloned_repo: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """latest mode still pulls the moving main tip (regression guard)."""
    clone, main_sha = cloned_repo
    _set_track_mode(monkeypatch, "latest")
    (clone / "f.txt").write_text("c2")
    _git(clone, "commit", "-am", "c2")
    _git(clone, "push", "origin", "main")
    result = g.git_pull_main()
    assert result.to_sha != main_sha
    assert _git(clone, "rev-parse", "HEAD") == result.to_sha


# ───────────── checkout atomicity (`_wait_index_lock_free` / `verify_tree_at`) ─────────────


def test_verify_tree_at_passes_on_clean_target(cloned_repo: tuple[Path, str]) -> None:
    """A checkout that really landed (HEAD == target, tree clean) verifies."""
    _clone, main_sha = cloned_repo
    g.verify_tree_at(main_sha, context="test")  # must not raise


def test_verify_tree_at_rejects_head_mismatch(cloned_repo: tuple[Path, str]) -> None:
    """HEAD moved away from the target (a concurrent checkout won the race) is a
    failed checkout, not a silent success."""
    clone, main_sha = cloned_repo
    (clone / "f.txt").write_text("c2")
    _git(clone, "commit", "-am", "c2")
    other_sha = _git(clone, "rev-parse", "HEAD")
    assert other_sha != main_sha
    with pytest.raises(g.GitPullFailed, match="HEAD is"):
        g.verify_tree_at(main_sha, context="test")


def test_verify_tree_at_rejects_mixed_tree(cloned_repo: tuple[Path, str]) -> None:
    """The 2026-08-02 poison: HEAD is the target but the working tree is not —
    a raced checkout left tracked files modified/missing. `verify_tree_at` must
    fail the update instead of letting `ava start` import the mixture."""
    clone, main_sha = cloned_repo
    (clone / "f.txt").write_text("corrupted by a racing checkout")
    with pytest.raises(g.GitPullFailed, match="working tree is not clean"):
        g.verify_tree_at(main_sha, context="test")


def test_verify_tree_at_ignores_untracked_strays(cloned_repo: tuple[Path, str]) -> None:
    """Untracked files are not the poison (a stray file is never imported);
    ignoring them keeps an operator's scratch file from failing every update."""
    clone, main_sha = cloned_repo
    (clone / "scratch.txt").write_text("stray")
    g.verify_tree_at(main_sha, context="test")  # must not raise


def test_wait_index_lock_free_blocks_on_held_lock(cloned_repo: tuple[Path, str]) -> None:
    """A held index.lock means another git process is mid-flight on this tree; a
    mutating op must fail loudly rather than race it (the lock only serializes
    the index — working-tree writes still interleave)."""
    clone, _main_sha = cloned_repo
    lock = clone / ".git" / "index.lock"
    lock.write_text("held by a concurrent checkout")
    with pytest.raises(g.GitPullFailed, match=r"index\.lock"):
        g._wait_index_lock_free(timeout_s=0.1)


def test_wait_index_lock_free_passes_when_free(cloned_repo: tuple[Path, str]) -> None:
    """No lock -> no wait, no raise."""
    _clone, _main_sha = cloned_repo
    g._wait_index_lock_free(timeout_s=0.1)


def test_git_checkout_sha_verifies_after_checkout(
    cloned_repo: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The atomicity check is wired into the checkout helper itself, so every
    caller (rollout local leg, agent-runner self-update) inherits it."""
    _clone, main_sha = cloned_repo
    calls: list[str] = []
    real = g.verify_tree_at

    def _spy(sha: str, *, context: str) -> None:
        calls.append(context)
        real(sha, context=context)

    monkeypatch.setattr(g, "verify_tree_at", _spy)
    g.git_checkout_sha(main_sha)
    assert "git_checkout_sha" in calls
