"""The `patch_targets` baseline section: introduced with its lint, shrink-only afterwards,
and carried by git renames when a test moves into `<pkg>/tests/`."""

from __future__ import annotations

import pathlib
import subprocess

import pytest

from scripts.lint import code_structure as lcs
from scripts.lint import patch_targets as lint
from scripts.structure import baseline_shards, locality, patch_targets
from tests.scripts.structure.patch_repo import make_repo, write

_LINT = "scripts/lint/patch_targets.py"
# A test whose subject spans two `base` packages (home `base`) and reaches into one's private.
_TEST = (
    "from base.net import retry\nfrom base.db import pool\n\n"
    "def test_x(monkeypatch):\n    retry.backoff()\n    pool.acquire()\n"
    "    monkeypatch.setattr('base.net.retry._sleep', None)\n"
)
_KEY = "tests/base/test_x.py::base.net.retry._sleep"
_MOVED_KEY = "base/tests/test_x.py::base.net.retry._sleep"


def _git(root: pathlib.Path, *args: str) -> str:
    return subprocess.run(  # noqa: S603 — fixed test commands, never external input
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=Structure gate test",
            "-c",
            "user.email=structure-test@example.invalid",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _freeze(root: pathlib.Path, counts: dict[str, int]) -> None:
    directory = root / baseline_shards.SHARD_DIR
    for stale in directory.glob("*.json"):
        stale.unlink()
    for name, shard in baseline_shards.split({patch_targets.SECTION: counts}).items():
        write(root, f"{baseline_shards.SHARD_DIR}/{name}.json", baseline_shards.render(shard))


def _sections(root: pathlib.Path, rev: str | None) -> dict[str, dict[str, int]]:
    """The merged baseline at `rev` (None: the working tree), the way the structure gate reads it."""
    if rev is None:
        return lcs._parse_baseline(baseline_shards.read_worktree(root))
    shards = locality.introduced(baseline_shards.read_at(root, rev), root, rev)
    assert shards is not None
    return lcs._parse_baseline(shards)


def _guard(root: pathlib.Path, rev: str, renames: dict[str, str] | None = None) -> list[str]:
    """`code_structure._baseline_guard` for this section, against `rev` in `root`."""
    previous = _sections(root, rev)[patch_targets.SECTION]
    current = _sections(root, None)[patch_targets.SECTION]
    remapped = lcs._remap_renamed_keys(patch_targets.SECTION, previous, renames or {})
    return lcs._section_guard(patch_targets.SECTION, current, remapped, renames=renames)


def _renames(root: pathlib.Path, rev: str) -> dict[str, str]:
    """Old -> new paths git -M detects between `rev` and the working tree."""
    out = _git(root, "diff", "-M", "--name-status", "--diff-filter=R", rev)
    pairs = (line.split("\t") for line in out.splitlines())
    return {old: new for _status, old, new in pairs}


@pytest.fixture
def repo(tmp_path: pathlib.Path) -> pathlib.Path:
    """A committed repository whose base revision already has the lint and a frozen site."""
    locality.reset_caches()
    root = make_repo(tmp_path, {"tests/base/test_x.py": _TEST, _LINT: "# the lint\n"})
    _freeze(root, {_KEY: 1})
    _git(root, "init", "--quiet")
    _git(root, "add", "-A")
    _git(root, "commit", "--quiet", "-m", "Freeze one patch-target site")
    return root


def test_the_lint_reads_its_own_frozen_site_as_clean(repo: pathlib.Path) -> None:
    assert lint.main([], repo_root=repo) == 0
    assert _guard(repo, "HEAD") == []


def test_a_new_key_is_refused_once_the_lint_exists_at_the_base(repo: pathlib.Path) -> None:
    write(repo, "tests/base/test_y.py", _TEST)
    _freeze(repo, {_KEY: 1, "tests/base/test_y.py::base.net.retry._sleep": 1})
    errors = _guard(repo, "HEAD")
    assert len(errors) == 1
    assert "added patch_targets entry tests/base/test_y.py::base.net.retry._sleep" in errors[0]


def test_a_raised_count_is_refused_and_a_lowered_one_is_fine(repo: pathlib.Path) -> None:
    _freeze(repo, {_KEY: 2})
    assert "raised patch_targets entry" in _guard(repo, "HEAD")[0]
    _freeze(repo, {})
    assert _guard(repo, "HEAD") == []


def test_the_section_is_introduced_with_the_change_that_adds_its_lint(
    tmp_path: pathlib.Path,
) -> None:
    root = make_repo(tmp_path, {"tests/base/test_x.py": _TEST})
    _git(root, "init", "--quiet")
    _git(root, "add", "-A")
    _git(root, "commit", "--quiet", "-m", "Before the lint")
    write(root, _LINT, "# the lint\n")
    _freeze(root, {_KEY: 1})
    assert _guard(root, "HEAD") == []  # nothing to shrink from yet: compared with itself
    _freeze(root, {_KEY: 1, "tests/base/test_y.py::base.net.retry._sleep": 1})
    assert _guard(root, "HEAD") == []  # still the introducing change
    _git(root, "add", "-A")
    _git(root, "commit", "--quiet", "-m", "Introduce the lint")
    _freeze(
        root, {_KEY: 1, "tests/base/test_y.py::base.net.retry._sleep": 1, "tests/z.py::a._b": 1}
    )
    assert "added patch_targets entry tests/z.py::a._b" in _guard(root, "HEAD")[0]


def test_introduced_leaves_the_other_sections_and_a_missing_base_alone(
    tmp_path: pathlib.Path,
) -> None:
    root = make_repo(tmp_path)
    _git(root, "init", "--quiet")
    _git(root, "add", "-A")
    _git(root, "commit", "--quiet", "-m", "Base")
    assert locality.introduced(None, root, "HEAD") is None
    shards = baseline_shards.read_at(root, "HEAD")
    assert shards == {}
    assert locality.introduced(shards, root, "HEAD") == {}


def test_a_moved_test_carries_its_frozen_key_no_more_and_no_fewer(repo: pathlib.Path) -> None:
    (repo / "base/tests").mkdir(parents=True)
    _git(repo, "mv", "tests/base/test_x.py", "base/tests/test_x.py")
    renames = _renames(repo, "HEAD")
    assert renames == {"tests/base/test_x.py": "base/tests/test_x.py"}

    # Not migrated: the lint sees a new site at the new path and a stale entry at the old one.
    assert lint.main([], repo_root=repo) == 1
    (error,) = _guard(repo, "HEAD", renames)
    assert f"was not migrated after its file moved to {_MOVED_KEY.partition('::')[0]}" in error

    # Migrated exactly: the verdict is unchanged by the move, and so is the baseline.
    _freeze(repo, {_MOVED_KEY: 1})
    assert lint.main([], repo_root=repo) == 0
    assert _guard(repo, "HEAD", renames) == []

    # More than was frozen is refused; fewer fails until the entry is lowered to reality.
    _freeze(repo, {_MOVED_KEY: 2})
    assert "raised patch_targets entry" in _guard(repo, "HEAD", renames)[0]
    assert lint.main([], repo_root=repo) == 1
