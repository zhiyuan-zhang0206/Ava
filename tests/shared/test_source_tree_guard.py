"""Regression guards for the source-tree integrity detector."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from shared import source_tree_guard as stg


def _git(source: Path, *args: str) -> str:
    return subprocess.run(  # noqa: S603
        ["git", "-C", str(source), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _init_source(source: Path) -> Path:
    """Create a real git repo at `source` with one commit on `main`; returns
    the checkout path."""
    source.mkdir(parents=True)
    _git(source, "init", "-b", "main")
    _git(source, "config", "user.email", "t@t")
    _git(source, "config", "user.name", "t")
    (source / "tracked.txt").write_text("x")
    _git(source, "add", ".")
    _git(source, "commit", "-m", "init")
    return source


def _commit(source: Path, content: str, msg: str) -> str:
    """Add a commit on the current branch from `content`, returning its sha."""
    (source / "tracked.txt").write_text(content)
    _git(source, "add", ".")
    _git(source, "commit", "-m", msg)
    return _git(source, "rev-parse", "HEAD")


# --- detector ---


def test_clean_checkout_reports_no_violations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("shared.paths.ava_home", lambda: tmp_path / "no-home")
    repo = _init_source(tmp_path / "source")

    assert stg.source_tree_violations(repo) == ()


def test_untracked_file_outside_whitelist_is_a_violation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("shared.paths.ava_home", lambda: tmp_path / "no-home")
    repo = _init_source(tmp_path / "source")
    (repo / "junk.txt").write_text("j")

    violations = stg.source_tree_violations(repo)

    assert any("junk.txt" in v for v in violations)


def test_untracked_dir_outside_whitelist_is_a_violation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("shared.paths.ava_home", lambda: tmp_path / "no-home")
    repo = _init_source(tmp_path / "source")
    (repo / "junkdir").mkdir()
    (repo / "junkdir" / "inner.txt").write_text("i")

    violations = stg.source_tree_violations(repo)

    assert any("junkdir" in v for v in violations)


def test_whitelisted_runtime_artifacts_are_not_violations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The built frontend bundle (frontend/) is the one legal untracked tree."""
    monkeypatch.setattr("shared.paths.ava_home", lambda: tmp_path / "no-home")
    repo = _init_source(tmp_path / "source")
    (repo / "frontend" / ".next").mkdir(parents=True)
    (repo / "frontend" / ".next" / "build.txt").write_text("b")
    (repo / "frontend" / "tsconfig.tsbuildinfo").write_text("t")

    assert stg.source_tree_violations(repo) == ()


def test_gitignored_paths_are_not_violations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gitignored runtime paths need no whitelist entry: the detector never
    sees them (the documented contract — e.g. the ci_accounting ledger at
    ``scripts/ci_usage/``)."""
    monkeypatch.setattr("shared.paths.ava_home", lambda: tmp_path / "no-home")
    repo = _init_source(tmp_path / "source")
    (repo / ".gitignore").write_text("scripts/ci_usage/\n")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-m", "ignore ci usage ledger")
    (repo / "scripts" / "ci_usage").mkdir(parents=True)
    (repo / "scripts" / "ci_usage" / "ledger.jsonl").write_text("{}\n")

    assert stg.source_tree_violations(repo) == ()


def test_tracked_modification_is_a_violation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("shared.paths.ava_home", lambda: tmp_path / "no-home")
    repo = _init_source(tmp_path / "source")
    (repo / "tracked.txt").write_text("changed")

    violations = stg.source_tree_violations(repo)

    assert any("tracked change" in v and "tracked.txt" in v for v in violations)


def test_head_move_alone_is_not_a_violation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A clean checkout whose HEAD moved is not tampering. No current lifecycle
    records an installed commit for a source checkout, so a legacy
    `$AVA_HOME/installed_sha` bookmark left behind by the retired updater must
    not turn a legitimate checkout move into a permanent alert."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr("shared.paths.ava_home", lambda: home)
    repo = _init_source(tmp_path / "source")
    (home / "installed_sha").write_text(_git(repo, "rev-parse", "HEAD") + "\n")
    _commit(repo, "y", "c2")

    assert stg.source_tree_violations(repo) == ()


def test_non_git_checkout_signals_guard_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A checkout that is not a git repo must not look clean: the guard
    cannot see anything, so it reports itself as skipped (not tampered)."""
    monkeypatch.setattr("shared.paths.ava_home", lambda: tmp_path / "no-home")
    repo = tmp_path / "plain"
    repo.mkdir()

    assert stg.source_tree_violations(repo) == ("guard skipped: not a git checkout",)


def _no_git(_source: Path, *_args: str, _timeout: float = 5.0) -> None:
    """Stand-in for a git binary that cannot run at all."""
    return


def test_git_unavailable_signals_guard_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A broken git (binary missing / command error / timeout) makes the
    detector blind — the probe must alert on the blind guard, never pass it."""
    monkeypatch.setattr("shared.paths.ava_home", lambda: tmp_path / "no-home")
    repo = _init_source(tmp_path / "source")
    monkeypatch.setattr(stg, "_git", _no_git)

    assert stg.source_tree_violations(repo) == ("guard skipped: git unavailable",)


def test_git_command_failure_signals_guard_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A git command that runs but exits non-zero is a blind guard too."""
    monkeypatch.setattr("shared.paths.ava_home", lambda: tmp_path / "no-home")
    repo = _init_source(tmp_path / "source")
    failed = subprocess.CompletedProcess([], returncode=1, stdout="", stderr="boom")

    def _git_fail(
        _source: Path, *_args: str, _timeout: float = 5.0
    ) -> subprocess.CompletedProcess[str]:
        return failed

    monkeypatch.setattr(stg, "_git", _git_fail)

    assert stg.source_tree_violations(repo) == ("guard skipped: git unavailable",)


# --- whitelist self-check ---


def test_whitelist_validation_rejects_empty() -> None:
    """An empty whitelist would flag every untracked file, the runtime artifacts
    included, as tamper — the guard must fail fast instead."""
    with pytest.raises(ValueError, match="must be non-empty"):
        stg._validate_whitelist(())


def test_whitelist_validation_rejects_catch_all_patterns() -> None:
    """A catch-all pattern whitelists arbitrary paths — detection would be
    always empty (the silent-no-op misconfiguration)."""
    for bad in (("*",), ("**",), ("*/*",), ("frontend/", "*")):
        with pytest.raises(ValueError, match="arbitrary paths"):
            stg._validate_whitelist(bad)


def test_whitelist_validation_accepts_real_whitelist() -> None:
    """The shipped whitelist stays sane: non-empty, no catch-all. Guards the
    constant against a future edit that silently silences the guard."""
    stg._validate_whitelist(stg.SOURCE_TREE_WHITELIST)


def test_whitelist_matches_only_runtime_artifacts() -> None:
    """Direct invariant for 'the whitelist must not swallow everything': a
    junk path is never whitelisted while the frontend bundle stays legal."""
    assert stg._is_whitelisted("junk.txt") is False
    assert stg._is_whitelisted("junkdir/inner.txt") is False
    assert stg._is_whitelisted("frontend/.next/build.txt") is True
    assert stg._is_whitelisted("frontend/") is True
