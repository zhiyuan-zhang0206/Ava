"""Immutable entry images and lazy versions retain the actually loaded generation.

Image reads never resolve Git. Independent entries capture independently; an
entry keeps its captured SHA when the checkout moves. Integer versions use and
cache that same captured SHA, with honest unknowns and bounded Git failures.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from base.native_process import code_version, loaded_commit

_GIT_ENV_KEYS = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_COMMON_DIR",
    "GIT_CEILING_DIRECTORIES",
)


@pytest.fixture(autouse=True)
def _isolated_code_version(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep Git free of the ambient repository variables a hook sets."""
    for key in _GIT_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def test_image_is_none_before_capture() -> None:
    """An entry with no captured SHA reports unknown without resolving HEAD.

    This is the guard against the original bug: any read path that can reach git
    is a read path that answers for the *current* checkout, not for the code the
    process is executing."""
    assert loaded_commit.LoadedCommit(Path.cwd(), None).sha is None


def test_image_never_shells_out(monkeypatch: pytest.MonkeyPatch) -> None:
    """Even with Git available and a moved checkout, an unknown fact stays silent."""

    def _explode(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("sha must not resolve Git")

    monkeypatch.setattr(subprocess, "run", _explode)
    assert loaded_commit.LoadedCommit(Path.cwd(), None).sha is None


def test_capture_captures_this_trees_head() -> None:
    """The capture is the commit of the tree the module was loaded from — the
    checkout under test, resolved from `__file__` rather than the cwd."""
    expected = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=Path(loaded_commit.__file__).resolve().parent.parent,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    image = loaded_commit.LoadedCommit.capture()
    assert image.sha == expected


def test_capture_keeps_the_first_answer_when_the_checkout_moves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Independent entry captures observe their own source generation.

    A later entry can load B while the first entry retains its immutable A fact."""
    shas = iter(["aaaaaaa1111", "bbbbbbb2222"])

    def _fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=[], returncode=0, stdout=next(shas) + "\n", stderr=""
        )

    monkeypatch.setattr(subprocess, "run", _fake_run)
    first = loaded_commit.LoadedCommit.capture()
    second = loaded_commit.LoadedCommit.capture()
    assert first.sha == "aaaaaaa1111"
    assert second.sha == "bbbbbbb2222"
    assert first.sha == "aaaaaaa1111"


def test_capture_is_none_outside_a_git_checkout(monkeypatch: pytest.MonkeyPatch) -> None:
    """A tarball / installed-package deploy has no commit; that is unknown, not
    a crash, and not a guess."""

    def _fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=[], returncode=128, stdout="", stderr="not a repo")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    assert loaded_commit.LoadedCommit.capture().sha is None
    assert loaded_commit.LoadedCommit(Path.cwd(), None).sha is None


def test_capture_survives_a_missing_git_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    """Capturing a commit is bookkeeping; it must never take a daemon down."""

    def _fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("git")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    assert loaded_commit.LoadedCommit.capture().sha is None


def test_capture_survives_a_hung_git(monkeypatch: pytest.MonkeyPatch) -> None:
    """The capture is bounded — a wedged git cannot stall a daemon's boot."""

    def _fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd="git", timeout=10)

    monkeypatch.setattr(subprocess, "run", _fake_run)
    assert loaded_commit.LoadedCommit.capture().sha is None


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(  # noqa: S603 — git with fixed arguments in a temp repo
        [
            "git",
            "-c",
            "user.name=test",
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _repo_with_commits(path: Path, count: int) -> Path:
    path.mkdir()
    _git(path, "init", "--initial-branch=main")
    for index in range(count):
        _git(path, "commit", "--allow-empty", "-m", f"commit {index}")
    return path


def test_counts_commits_on_the_first_parent_line(tmp_path: Path) -> None:
    repo = _repo_with_commits(tmp_path / "repo", 3)
    assert code_version.first_parent_count(repo) == 3
    _git(repo, "commit", "--allow-empty", "-m", "fourth")
    assert code_version.first_parent_count(repo) == 4


def test_a_merged_side_branch_adds_only_the_merge_commit(tmp_path: Path) -> None:
    """Three commits on main, three on a side branch merged with --no-ff: the
    first-parent line holds the three plus the merge, not the side branch's own."""
    repo = _repo_with_commits(tmp_path / "repo", 3)
    _git(repo, "switch", "--create", "side")
    for index in range(3):
        _git(repo, "commit", "--allow-empty", "-m", f"side {index}")
    _git(repo, "switch", "main")
    _git(repo, "merge", "--no-ff", "-m", "merge side", "side")

    assert int(_git(repo, "rev-list", "--count", "HEAD")) == 7
    assert code_version.first_parent_count(repo) == 4


def test_counts_from_the_named_revision_not_head(tmp_path: Path) -> None:
    repo = _repo_with_commits(tmp_path / "repo", 3)
    earlier = _git(repo, "rev-parse", "HEAD~1")
    _git(repo, "commit", "--allow-empty", "-m", "moves HEAD on")

    assert code_version.first_parent_count(repo, earlier) == 2
    assert code_version.first_parent_count(repo) == 4


def test_a_directory_that_is_not_a_checkout_fails_fast(tmp_path: Path) -> None:
    bare = tmp_path / "not-a-repo"
    bare.mkdir()
    with pytest.raises(code_version.CodeVersionError, match="failed"):
        code_version.first_parent_count(bare)


def test_an_unresolvable_revision_fails_fast(tmp_path: Path) -> None:
    repo = _repo_with_commits(tmp_path / "repo", 1)
    with pytest.raises(code_version.CodeVersionError, match="failed"):
        code_version.first_parent_count(repo, "no-such-revision")


def test_image_counts_from_the_frozen_commit_and_caches(monkeypatch: pytest.MonkeyPatch) -> None:
    """The version answers for the commit the process loaded (`capture()`), asked of
    this module's own tree, and one version owner asks Git once."""
    asked: list[tuple[Path, str]] = []

    def fake_count(repo: Path, rev: str = "HEAD") -> int:
        asked.append((repo, rev))
        return 41

    image = loaded_commit.LoadedCommit(Path.cwd(), "abc123")
    version = code_version.CodeVersion(image)
    monkeypatch.setattr(code_version, "first_parent_count", fake_count)

    assert version.get() == 41
    assert version.get() == 41
    assert asked == [(image.source_root, "abc123")]


def test_image_without_a_git_checkout_fails_fast_and_is_not_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image = loaded_commit.LoadedCommit(Path.cwd(), None)
    with pytest.raises(code_version.CodeVersionError, match="loaded commit"):
        code_version.CodeVersion(image).get()


def test_image_matches_git_for_this_checkout() -> None:
    """End to end against the checkout under test (shallow in CI, so only the
    agreement with git is asserted, never a magnitude)."""
    image = loaded_commit.LoadedCommit.capture()
    assert image.sha is not None
    expected = _git(image.source_root, "rev-list", "--count", "--first-parent", image.sha)
    version = code_version.CodeVersion(image)
    assert version.get() == int(expected)
    assert version.get() >= 1


def test_explicit_loaded_image_survives_checkout_move_before_lazy_version(
    tmp_path: Path,
) -> None:
    repo = _repo_with_commits(tmp_path / "explicit-image", 2)
    loaded = loaded_commit.LoadedCommit.capture(repo)
    sha = loaded.sha
    version = code_version.CodeVersion(loaded)
    _git(repo, "commit", "--allow-empty", "-m", "replacement image")
    assert _git(repo, "rev-parse", "HEAD") != sha
    assert loaded.sha == sha
    assert version.get() == 2
    _git(repo, "commit", "--allow-empty", "-m", "another replacement")
    assert version.get() == 2


def test_unknown_loaded_image_cannot_be_replaced_by_a_later_git_checkout(
    tmp_path: Path,
) -> None:
    source = tmp_path / "initially-unknown"
    source.mkdir()
    loaded = loaded_commit.LoadedCommit.capture(source)
    assert loaded.sha is None
    _git(source, "init", "--initial-branch=main")
    _git(source, "commit", "--allow-empty", "-m", "late checkout")
    with pytest.raises(code_version.CodeVersionError, match="loaded commit"):
        code_version.CodeVersion(loaded).get()
    assert loaded.sha is None
