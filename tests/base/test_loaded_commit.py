"""Process-commit capture — the properties that make it process state.

Two of these tests are the whole point of the module and are worth stating
plainly, because a plausible-looking implementation passes everything else and
still reproduces the bug it exists to prevent:

- `get()` never reads git. An implementation that lazily resolves HEAD on first
  read answers with whatever the checkout became, so a daemon that outlived a
  rollout would report the *new* commit and look aligned.
- `freeze()` keeps its first answer. The capture is meant to describe the code
  the process loaded, which cannot change without a restart; a re-reading
  `freeze()` would let a later caller overwrite that with a newer commit.

The code version (`base.native_process.code_version`) is derived from that
capture, so its tests live here too: the first-parent commit count follows the
first-parent line only, comes from the commit the process loaded and not from
whatever HEAD became, is computed once per process, and a tree with no git
history fails fast instead of reporting 0.
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
def _fresh_capture():
    """Each test starts from an unfrozen process and leaves one behind."""
    loaded_commit._reset_for_tests()
    yield
    loaded_commit._reset_for_tests()


@pytest.fixture(autouse=True)
def _isolated_code_version(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each test starts with no cached code version and no gate posture (both are
    restored after), and git free of the ambient repository variables a hook sets."""
    monkeypatch.setattr(code_version, "_version", None)
    monkeypatch.setattr(code_version, "_db_gate_exempt", False)
    for key in _GIT_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def test_get_is_none_before_freeze() -> None:
    """An unfrozen process reports unknown rather than resolving HEAD on demand.

    This is the guard against the original bug: any read path that can reach git
    is a read path that answers for the *current* checkout, not for the code the
    process is executing."""
    assert loaded_commit.get() is None


def test_get_never_shells_out(monkeypatch: pytest.MonkeyPatch) -> None:
    """Even with git available and a moved checkout, `get()` stays silent until
    someone froze — it has no git call to make."""

    def _explode(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("get() must not resolve git")

    monkeypatch.setattr(subprocess, "run", _explode)
    assert loaded_commit.get() is None


def test_freeze_captures_this_trees_head() -> None:
    """The capture is the commit of the tree the module was loaded from — the
    checkout under test, resolved from `__file__` rather than the cwd."""
    expected = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=Path(loaded_commit.__file__).resolve().parent.parent,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert loaded_commit.freeze() == expected
    assert loaded_commit.get() == expected


def test_freeze_keeps_the_first_answer_when_the_checkout_moves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second freeze after the checkout advanced re-uses the first capture.

    Stand-in for the real sequence: a daemon boots on commit A, a rollout moves
    the checkout to B, and something in-process calls freeze() again. The daemon
    is still executing A, so A is the only honest answer."""
    shas = iter(["aaaaaaa1111", "bbbbbbb2222"])

    def _fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=[], returncode=0, stdout=next(shas) + "\n", stderr=""
        )

    monkeypatch.setattr(subprocess, "run", _fake_run)
    assert loaded_commit.freeze() == "aaaaaaa1111"
    assert loaded_commit.freeze() == "aaaaaaa1111"
    assert loaded_commit.get() == "aaaaaaa1111"


def test_freeze_is_none_outside_a_git_checkout(monkeypatch: pytest.MonkeyPatch) -> None:
    """A tarball / installed-package deploy has no commit; that is unknown, not
    a crash, and not a guess."""

    def _fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=[], returncode=128, stdout="", stderr="not a repo")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    assert loaded_commit.freeze() is None
    assert loaded_commit.get() is None


def test_freeze_survives_a_missing_git_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    """Capturing a commit is bookkeeping; it must never take a daemon down."""

    def _fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("git")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    assert loaded_commit.freeze() is None


def test_freeze_survives_a_hung_git(monkeypatch: pytest.MonkeyPatch) -> None:
    """The capture is bounded — a wedged git cannot stall a daemon's boot."""

    def _fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd="git", timeout=10)

    monkeypatch.setattr(subprocess, "run", _fake_run)
    assert loaded_commit.freeze() is None


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


def test_get_counts_from_the_frozen_commit_and_caches(monkeypatch: pytest.MonkeyPatch) -> None:
    """The version answers for the commit the process loaded (`freeze()`), asked of
    this module's own tree, and one process asks git once."""
    asked: list[tuple[Path, str]] = []

    def fake_count(repo: Path, rev: str = "HEAD") -> int:
        asked.append((repo, rev))
        return 41

    monkeypatch.setattr(loaded_commit, "freeze", lambda: "abc123")
    monkeypatch.setattr(code_version, "first_parent_count", fake_count)

    assert code_version.get() == 41
    assert code_version.get() == 41
    assert asked == [(code_version._SOURCE_ROOT, "abc123")]


def test_get_without_a_git_checkout_fails_fast_and_is_not_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(loaded_commit, "freeze", lambda: None)
    with pytest.raises(code_version.CodeVersionError, match="git checkout"):
        code_version.get()


def test_get_matches_git_for_this_checkout() -> None:
    """End to end against the checkout under test (shallow in CI, so only the
    agreement with git is asserted, never a magnitude)."""
    expected = subprocess.run(
        ["git", "rev-list", "--count", "--first-parent", "HEAD"],
        cwd=code_version._SOURCE_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert code_version.get() == int(expected)
    assert code_version.get() >= 1


def test_the_database_gate_applies_until_a_process_exempts_itself() -> None:
    assert code_version.db_gate_applies() is True
    code_version.exempt_from_db_gate()
    assert code_version.db_gate_applies() is False
