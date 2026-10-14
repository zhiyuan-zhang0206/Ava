"""`debt carry`: recorded debt follows a file that git sees as renamed."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from meizel.tests.synthetic import RepoFactory, analysis_of, commit, failures, git, run, write

LIB = {
    "lib/__init__.py": "",
    "lib/mod.py": "_hidden = 1\n_other = 2\n",
    "lib/meizel.toml": 'exports = ["mod.py"]\n',
    "old/__init__.py": "",
    "old/keep.py": "from lib import mod\n",
    "new/__init__.py": "",
    "new/keep.py": "from lib import mod\n",
}
TEST = "from lib.mod import _hidden\n\n\ndef test_a():\n    assert _hidden\n"
ROOT = '[uses]\n"new" = ["lib"]\n"old" = ["lib"]\n\n[debt.private-name]\n"old/test_x.py -> lib/mod.py#_hidden" = 1\n'


def debt_of(root: Path, directory: str) -> dict[str, dict[str, int]]:
    path = root / directory / "meizel.toml" if directory else root / "meizel.toml"
    return tomllib.loads(path.read_text()).get("debt", {}) if path.exists() else {}


def carry(root: Path, *flags: str) -> int:
    code = run(root, "debt", "carry", *flags)
    git(root, "add", "-A")
    return code


def moved_repo(make_repo: RepoFactory, extra: dict[str, str] | None = None) -> Path:
    root = make_repo({**LIB, "old/test_x.py": TEST, "meizel.toml": ROOT, **(extra or {})})
    commit(root)
    return root


def test_a_moved_file_takes_its_debt_along(make_repo: RepoFactory) -> None:
    root = moved_repo(make_repo)
    git(root, "mv", "old/test_x.py", "new/test_x.py")
    assert ("debt-paid", "meizel.toml") in failures(root)
    assert carry(root) == 0
    assert debt_of(root, "") == {"private-name": {"new/test_x.py -> lib/mod.py#_hidden": 1}}
    assert failures(root) == set()


def test_the_owner_follows_the_move_into_another_directory(make_repo: RepoFactory) -> None:
    root = moved_repo(make_repo)
    (root / "lib/tests").mkdir()
    git(root, "mv", "old/test_x.py", "lib/tests/test_x.py")
    write(
        root,
        {
            "lib/tests/__init__.py": "",
            "lib/meizel.toml": 'exports = ["mod.py"]\n\n[uses]\n"tests" = ["mod.py"]\n',
        },
    )
    assert carry(root) == 0
    assert debt_of(root, "") == {}
    assert debt_of(root, "lib") == {"private-name": {"tests/test_x.py -> mod.py#_hidden": 1}}
    assert failures(root) == set()


def test_a_moved_target_rewrites_the_key_of_the_file_that_uses_it(make_repo: RepoFactory) -> None:
    root = moved_repo(make_repo)
    git(root, "mv", "lib/mod.py", "lib/mod2.py")
    write(
        root,
        {
            "old/test_x.py": TEST.replace("lib.mod", "lib.mod2"),
            "lib/meizel.toml": 'exports = ["mod2.py"]\n',
        },
    )
    assert carry(root) == 0
    assert debt_of(root, "") == {"private-name": {"old/test_x.py -> lib/mod2.py#_hidden": 1}}
    assert failures(root) == set()


def test_file_scoped_debt_moves_with_the_file(make_repo: RepoFactory) -> None:
    files = {
        "old/test_p.py": "from lib import mod\n\n\ndef test_a(monkeypatch):\n    monkeypatch.setattr(mod, 'x', 1)\n",
        "old/meizel.toml": '[debt.restricted-python]\n"test_p.py#exec of source text" = 1\n',
    }
    root = moved_repo(make_repo, files)
    git(root, "mv", "old/test_p.py", "new/test_p.py")
    assert carry(root) == 0
    assert debt_of(root, "old") == {}
    assert debt_of(root, "new") == {"restricted-python": {"test_p.py#exec of source text": 1}}


def test_a_directory_move_keeps_relative_entries_and_remaps_outside_ones(
    make_repo: RepoFactory,
) -> None:
    files = {
        "old/meizel.toml": '[debt.restricted-python]\n"test_x.py#exec of source text" = 1\n',
    }
    root = moved_repo(make_repo, files)
    git(root, "mv", "old/test_x.py", "new/test_x.py")
    git(root, "mv", "old/meizel.toml", "new/meizel.toml")
    assert carry(root) == 0
    assert debt_of(root, "new") == {"restricted-python": {"test_x.py#exec of source text": 1}}
    assert debt_of(root, "") == {"private-name": {"new/test_x.py -> lib/mod.py#_hidden": 1}}


def test_counts_merge_into_an_existing_entry_and_the_total_never_grows(
    make_repo: RepoFactory,
) -> None:
    root_manifest = ROOT + '"new/test_x.py -> lib/mod.py#_hidden" = 1\n'
    root = moved_repo(make_repo, {"meizel.toml": root_manifest})
    git(root, "mv", "old/test_x.py", "new/test_x.py")
    assert carry(root) == 0
    assert debt_of(root, "") == {"private-name": {"new/test_x.py -> lib/mod.py#_hidden": 2}}


def test_a_new_violation_in_the_moved_file_is_not_recorded(make_repo: RepoFactory) -> None:
    padding = "".join(f"def test_{n}():\n    assert {n} == {n}\n\n\n" for n in range(12))
    root = moved_repo(make_repo, {"old/test_x.py": TEST + "\n\n" + padding})
    git(root, "mv", "old/test_x.py", "new/test_x.py")
    changed = (
        (root / "new/test_x.py").read_text().replace("import _hidden\n", "import _hidden, _other\n")
    )
    write(root, {"new/test_x.py": changed})
    assert carry(root) == 0
    assert debt_of(root, "") == {"private-name": {"new/test_x.py -> lib/mod.py#_hidden": 1}}
    assert ("private-name", "new/test_x.py") in failures(root)


def test_debt_of_a_file_that_was_not_renamed_stays(make_repo: RepoFactory) -> None:
    root = moved_repo(make_repo)
    write(root, {"old/other.py": "VALUE = 1\n"})
    before = (root / "meizel.toml").read_text()
    assert carry(root) == 0
    assert (root / "meizel.toml").read_text() == before


def test_a_file_that_still_exists_at_its_old_path_keeps_its_debt(make_repo: RepoFactory) -> None:
    root = moved_repo(make_repo)
    write(root, {"new/test_x.py": TEST})
    before = (root / "meizel.toml").read_text()
    assert carry(root) == 0
    assert (root / "meizel.toml").read_text() == before


def test_carry_does_nothing_without_a_rename_and_says_so(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    root = moved_repo(make_repo)
    assert carry(root) == 0
    assert "nothing to carry" in capsys.readouterr().out


def test_a_move_git_cannot_see_is_not_carried(make_repo: RepoFactory) -> None:
    root = moved_repo(make_repo)
    (root / "old/test_x.py").rename(root / "new/test_x.py")
    assert run(root, "debt", "carry") == 0
    assert debt_of(root, "") == {"private-name": {"old/test_x.py -> lib/mod.py#_hidden": 1}}


def test_the_base_can_be_named(make_repo: RepoFactory) -> None:
    root = moved_repo(make_repo)
    git(root, "mv", "old/test_x.py", "new/test_x.py")
    commit(root, "move")
    assert carry(root) == 0
    assert debt_of(root, "") == {"private-name": {"old/test_x.py -> lib/mod.py#_hidden": 1}}
    assert carry(root, "--base", "HEAD~1") == 0
    assert debt_of(root, "") == {"private-name": {"new/test_x.py -> lib/mod.py#_hidden": 1}}


def test_prune_afterwards_leaves_nothing_to_do(make_repo: RepoFactory) -> None:
    root = moved_repo(make_repo)
    git(root, "mv", "old/test_x.py", "new/test_x.py")
    assert carry(root) == 0
    before = (root / "meizel.toml").read_text()
    assert run(root, "debt", "prune") == 0
    assert (root / "meizel.toml").read_text() == before
    assert analysis_of(root).verdict == []


PATCHED = (
    "from lib import mod\n\n\n"
    "def test_a(monkeypatch):\n"
    "    monkeypatch.setattr(mod.helper, 'x', 1)\n"
    "    monkeypatch.setattr(mod.helper, 'y', 1)\n"
)


def patch_repo(make_repo: RepoFactory) -> Path:
    baseline = (
        "[debt.first-party-patch]\n"
        '"test_p.py#monkeypatch.setattr lib.mod.helper.x" = 1\n'
        '"test_p.py#monkeypatch.setattr lib.mod.helper.y" = 1\n'
        '"test_p.py#monkeypatch.setattr lib.mod.other" = 4\n'
    )
    files = {**LIB, "old/test_p.py": PATCHED, "old/meizel.toml": baseline}
    root = make_repo(files)
    commit(root)
    return root


def test_a_renamed_patch_target_is_carried_one_for_one(make_repo: RepoFactory) -> None:
    root = patch_repo(make_repo)
    assert carry(root, "--rename-target", "lib.mod.helper=lib.mod.Helper") == 0
    assert debt_of(root, "old")["first-party-patch"] == {
        "test_p.py#monkeypatch.setattr lib.mod.Helper.x": 1,
        "test_p.py#monkeypatch.setattr lib.mod.Helper.y": 1,
        "test_p.py#monkeypatch.setattr lib.mod.other": 4,
    }


def test_a_renamed_target_never_changes_the_total_or_the_source_file(
    make_repo: RepoFactory,
) -> None:
    root = patch_repo(make_repo)
    write(
        root,
        {
            "old/meizel.toml": (root / "old/meizel.toml").read_text()
            + '"test_p.py#monkeypatch.setattr lib.mod.Helper.x" = 2\n'
        },
    )
    assert carry(root, "--rename-target", "lib.mod.helper.x=lib.mod.Helper.x") == 0
    rows = debt_of(root, "old")["first-party-patch"]
    assert rows["test_p.py#monkeypatch.setattr lib.mod.Helper.x"] == 3
    assert sum(rows.values()) == 8


def test_a_target_rename_does_not_touch_private_names_or_other_names(
    make_repo: RepoFactory,
) -> None:
    root = moved_repo(make_repo)
    before = (root / "meizel.toml").read_text()
    assert carry(root, "--rename-target", "lib.mod=lib.mod2") == 0
    assert (root / "meizel.toml").read_text() == before


def test_a_target_rename_must_be_written_old_equals_new(make_repo: RepoFactory) -> None:
    root = patch_repo(make_repo)
    assert run(root, "debt", "carry", "--rename-target", "lib.mod.helper") == 2
    assert run(root, "debt", "carry", "--rename-target", "a=a") == 2
