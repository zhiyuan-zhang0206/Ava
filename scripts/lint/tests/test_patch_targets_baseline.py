"""The `patch_targets` baseline section: introduced with its lint, shrink-only afterwards,
carried by git renames when a test moves into `<pkg>/tests/`, and re-frozen under a new rule
version when the placement rule changes."""

from __future__ import annotations

import json
import pathlib
import subprocess

import pytest

from scripts.lint import code_structure as lcs
from scripts.lint import patch_targets as lint
from scripts.structure import baseline_shards, locality, patch_targets
from scripts.structure.tests.patch_repo import make_repo, write

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


def _freeze(
    root: pathlib.Path, counts: dict[str, int], *, rules: dict[str, int] | None = None
) -> None:
    directory = root / baseline_shards.SHARD_DIR
    for stale in directory.glob("*.json"):
        stale.unlink()
    for name, shard in baseline_shards.split({patch_targets.SECTION: counts}).items():
        write(root, f"{baseline_shards.SHARD_DIR}/{name}.json", baseline_shards.render(shard))
    if rules is not None:
        write(root, f"{baseline_shards.SHARD_DIR}/{baseline_shards.RULES_FILE}", json.dumps(rules))


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


# ------------------------------------------------------------------ rule versions
# A change to how a section is measured re-freezes it under a higher version in `rules.json`;
# across that one change the structure gate holds the section's total, not its keys.

_OLD = {
    "tests/base/test_a.py::base.net.retry._sleep": 3,
    "tests/base/test_b.py::base.net.retry._sleep": 2,
    "tests/base/test_c.py::base.db.pool._pool": 1,
}
_TOTAL = sum(_OLD.values())
_NEW_KEY = "tests/cli/test_e.py::cli.commands._util._helper"


@pytest.fixture
def gate(repo: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    """`repo` with three frozen keys committed at rule version 1, as the structure gate's root."""
    monkeypatch.setattr(lcs, "_REPO_ROOT", repo)
    monkeypatch.delenv("LINT_STRUCTURE_BASELINE_BASE", raising=False)
    _freeze(repo, _OLD)
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", "Three frozen keys")
    return repo


def _gate(capsys: pytest.CaptureFixture[str]) -> tuple[int, str]:
    return lcs.main([]), capsys.readouterr().out


def test_at_the_same_version_a_new_key_is_refused_as_before(
    gate: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _freeze(gate, {**_OLD, _NEW_KEY: 1})
    status, out = _gate(capsys)
    assert status == 1
    assert f"added patch_targets entry {_NEW_KEY}" in out


def test_a_raised_version_with_a_lower_total_accepts_new_and_vanished_keys(
    gate: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fewer = {"tests/base/test_a.py::base.net.retry._sleep": 1, _NEW_KEY: 2}
    assert sum(fewer.values()) < _TOTAL
    _freeze(gate, fewer, rules={patch_targets.SECTION: 2})
    assert _gate(capsys) == (0, "")


def test_a_raised_version_may_keep_the_total_exactly(
    gate: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _freeze(gate, {_NEW_KEY: _TOTAL}, rules={patch_targets.SECTION: 2})
    assert _gate(capsys) == (0, "")


def test_a_raised_version_with_a_higher_total_is_refused(
    gate: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _freeze(gate, {_NEW_KEY: _TOTAL + 1}, rules={patch_targets.SECTION: 2})
    status, out = _gate(capsys)
    assert status == 1
    assert "rule version rose from 1 to 2, but the frozen total rose " in out
    assert f"from {_TOTAL} to {_TOTAL + 1}" in out


def test_once_the_new_version_is_the_base_the_keys_are_guarded_again(
    gate: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    refrozen = {_NEW_KEY: 2}
    _freeze(gate, refrozen, rules={patch_targets.SECTION: 2})
    assert _gate(capsys) == (0, "")
    _git(gate, "add", "-A")
    _git(gate, "commit", "--quiet", "-m", "Re-freeze under rule version 2")

    _freeze(
        gate,
        {**refrozen, "tests/cli/test_f.py::cli.commands._util._other": 1},
        rules={patch_targets.SECTION: 2},
    )
    status, out = _gate(capsys)
    assert status == 1
    assert "added patch_targets entry tests/cli/test_f.py::cli.commands._util._other" in out

    _freeze(gate, {_NEW_KEY: 3}, rules={patch_targets.SECTION: 2})
    status, out = _gate(capsys)
    assert status == 1
    assert f"raised patch_targets entry {_NEW_KEY} from 2 to 3" in out


def test_the_version_only_goes_up(gate: pathlib.Path, capsys: pytest.CaptureFixture[str]) -> None:
    _freeze(gate, _OLD, rules={patch_targets.SECTION: 2})
    _git(gate, "add", "-A")
    _git(gate, "commit", "--quiet", "-m", "Version 2")
    _freeze(gate, _OLD, rules={patch_targets.SECTION: 1})
    status, out = _gate(capsys)
    assert status == 1
    assert "patch_targets rule version went back from 2 to 1" in out


def test_a_version_belongs_to_its_section(
    gate: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Raising `patch_targets` does not relax another section's guard."""
    _freeze(gate, {_NEW_KEY: 1}, rules={patch_targets.SECTION: 2})
    write(
        gate,
        f"{baseline_shards.SHARD_DIR}/base.json",
        '{"private_imports": {"base/x.py::base._y": 1}}',
    )
    status, out = _gate(capsys)
    assert status == 1
    assert "added private_imports entry base/x.py::base._y" in out
    assert "rule version" not in out


@pytest.mark.parametrize("bad", ['{"patch_targets": 0}', '{"patch_targets": "2"}', "[2]", "nope"])
def test_a_malformed_rules_file_is_an_error(
    gate: pathlib.Path, capsys: pytest.CaptureFixture[str], bad: str
) -> None:
    write(gate, f"{baseline_shards.SHARD_DIR}/{baseline_shards.RULES_FILE}", bad)
    status, out = _gate(capsys)
    assert status == 1
    assert "invalid rule versions" in out


def test_the_rules_file_is_not_a_shard(gate: pathlib.Path) -> None:
    name = baseline_shards.RULES_FILE.removesuffix(".json")
    _freeze(gate, _OLD, rules={patch_targets.SECTION: 2})
    assert name not in baseline_shards.read_worktree(gate)
    assert baseline_shards.read_rules_worktree(gate) == {patch_targets.SECTION: 2}
    _git(gate, "add", "-A")
    _git(gate, "commit", "--quiet", "-m", "Version 2")
    assert name not in (baseline_shards.read_at(gate, "HEAD") or {})
    assert baseline_shards.read_rules_at(gate, "HEAD") == {patch_targets.SECTION: 2}
    assert baseline_shards.read_rules_at(gate, "HEAD~1") == {}
