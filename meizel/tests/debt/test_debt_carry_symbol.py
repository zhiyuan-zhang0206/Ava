from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from meizel.tests.synthetic import RepoFactory, commit, failures, git, run, write

LIB = {
    "lib/__init__.py": "",
    "lib/mod.py": "_hidden = 1\n\n\ndef helper() -> int:\n    return 1\n\n\ndef other() -> int:\n    return 2\n",
    "lib/meizel.toml": 'exports = ["mod.py"]\n',
    "app/__init__.py": "",
    "new/__init__.py": "",
    "new/keep.py": "from lib import mod\n",
}
ROOT = '[uses]\n"app" = ["lib"]\n"new" = ["lib"]\n'
FIXTURE = (
    "@pytest.fixture(autouse=True)\n"
    "def fake(monkeypatch):\n"
    "    monkeypatch.setattr(mod, 'helper', lambda: 1)\n"
    "    monkeypatch.setattr(mod, 'other', lambda: 1)\n"
)
TEST = "def test_a(monkeypatch):\n    monkeypatch.setattr(mod, 'helper', lambda: 2)\n"
HEADER = "import pytest\nfrom lib import mod\n\n\n"
PATCH_DEBT = (
    "[debt.first-party-patch]\n"
    '"test_x.py#monkeypatch.setattr lib.mod.helper" = 2\n'
    '"test_x.py#monkeypatch.setattr lib.mod.other" = 1\n'
)
HELPER = "monkeypatch.setattr lib.mod.helper"
OTHER = "monkeypatch.setattr lib.mod.other"


def debt_of(root: Path, directory: str) -> dict[str, dict[str, int]]:
    path = root / directory / "meizel.toml" if directory else root / "meizel.toml"
    return tomllib.loads(path.read_text()).get("debt", {}) if path.exists() else {}


def patch_rows(root: Path, directory: str) -> dict[str, int]:
    return debt_of(root, directory).get("first-party-patch", {})


def fixture_repo(make_repo: RepoFactory) -> Path:
    files = {
        **LIB,
        "meizel.toml": ROOT,
        "app/meizel.toml": PATCH_DEBT,
        "app/test_x.py": f"{HEADER}{FIXTURE}\n\n{TEST}",
    }
    root = make_repo(files)
    commit(root)
    return root


def move_fixture(root: Path, target: str = "app/calls.py") -> None:
    write(root, {"app/test_x.py": f"{HEADER}{TEST}", target: f"{HEADER}{FIXTURE}"})
    git(root, "add", "-A")


def carry(root: Path, *flags: str) -> int:
    code = run(root, "debt", "carry", *flags)
    git(root, "add", "-A")
    return code


def test_a_moved_function_takes_only_its_own_entries(make_repo: RepoFactory) -> None:
    root = fixture_repo(make_repo)
    move_fixture(root)
    assert failures(root) != set()
    assert carry(root, "--move-symbol", "app/test_x.py::fake=app/calls.py") == 0
    assert patch_rows(root, "app") == {
        f"test_x.py#{HELPER}": 1,
        f"calls.py#{HELPER}": 1,
        f"calls.py#{OTHER}": 1,
    }
    assert failures(root) == set()


def test_the_owner_follows_a_symbol_into_another_directory(make_repo: RepoFactory) -> None:
    root = fixture_repo(make_repo)
    move_fixture(root, "new/calls.py")
    assert carry(root, "--move-symbol", "app/test_x.py::fake=new/calls.py") == 0
    assert patch_rows(root, "app") == {f"test_x.py#{HELPER}": 1}
    assert patch_rows(root, "new") == {f"calls.py#{HELPER}": 1, f"calls.py#{OTHER}": 1}
    assert failures(root) == set()


def test_the_recorded_total_of_a_code_never_changes(make_repo: RepoFactory) -> None:
    root = fixture_repo(make_repo)
    before = sum(patch_rows(root, "app").values())
    move_fixture(root)
    assert carry(root, "--move-symbol", "app/test_x.py::fake=app/calls.py") == 0
    assert sum(patch_rows(root, "app").values()) == before


def test_a_private_name_imported_inside_the_moved_function_moves_too(
    make_repo: RepoFactory,
) -> None:
    reader = "def read_hidden():\n    from lib.mod import _hidden\n\n    return _hidden\n"
    top = "from lib.mod import _hidden\n\n\n"
    files = {
        **LIB,
        "meizel.toml": ROOT + '\n[debt.private-name]\n"app/test_x.py -> lib/mod.py#_hidden" = 2\n',
        "app/test_x.py": f"{top}{reader}\n\ndef test_a():\n    assert _hidden\n",
    }
    root = make_repo(files)
    commit(root)
    write(
        root,
        {
            "app/test_x.py": f"{top}def test_a():\n    assert _hidden\n",
            "app/calls.py": reader,
        },
    )
    git(root, "add", "-A")
    assert carry(root, "--move-symbol", "app/test_x.py::read_hidden=app/calls.py") == 0
    assert debt_of(root, "") == {
        "private-name": {
            "app/test_x.py -> lib/mod.py#_hidden": 1,
            "app/calls.py -> lib/mod.py#_hidden": 1,
        }
    }
    assert failures(root) == set()


def test_a_class_moves_like_a_function(make_repo: RepoFactory) -> None:
    holder = "class Holder:\n    def swap(self, monkeypatch):\n        monkeypatch.setattr(mod, 'other', lambda: 3)\n"
    files = {
        **LIB,
        "meizel.toml": ROOT,
        "app/meizel.toml": f'[debt.first-party-patch]\n"test_x.py#{HELPER}" = 1\n"test_x.py#{OTHER}" = 1\n',
        "app/test_x.py": f"{HEADER}{holder}\n\n{TEST}",
    }
    root = make_repo(files)
    commit(root)
    write(root, {"app/test_x.py": f"{HEADER}{TEST}", "app/holder.py": f"{HEADER}{holder}"})
    git(root, "add", "-A")
    assert carry(root, "--move-symbol", "app/test_x.py::Holder=app/holder.py") == 0
    assert patch_rows(root, "app") == {f"test_x.py#{HELPER}": 1, f"holder.py#{OTHER}": 1}
    assert failures(root) == set()


def test_a_violation_the_symbol_gained_while_moving_is_not_carried(make_repo: RepoFactory) -> None:
    root = fixture_repo(make_repo)
    move_fixture(root)
    gained = f"{HEADER}{FIXTURE}    monkeypatch.setattr(mod, 'other', lambda: 2)\n"
    write(root, {"app/calls.py": gained})
    git(root, "add", "-A")
    assert carry(root, "--move-symbol", "app/test_x.py::fake=app/calls.py") == 0
    assert patch_rows(root, "app")[f"calls.py#{OTHER}"] == 1
    assert ("first-party-patch", "app/calls.py") in failures(root)


def test_entries_that_are_not_the_symbols_stay_put(make_repo: RepoFactory) -> None:
    root = fixture_repo(make_repo)
    move_fixture(root)
    assert carry(root, "--move-symbol", "app/test_x.py::fake=app/calls.py") == 0
    assert patch_rows(root, "app")[f"test_x.py#{HELPER}"] == 1


def test_a_symbol_without_debt_changes_nothing(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    quiet = "def quiet() -> int:\n    return 1\n"
    files = {
        **LIB,
        "meizel.toml": ROOT,
        "app/meizel.toml": f'[debt.first-party-patch]\n"test_x.py#{HELPER}" = 1\n',
        "app/test_x.py": f"{HEADER}{quiet}\n\n{TEST}",
    }
    root = make_repo(files)
    commit(root)
    write(root, {"app/test_x.py": f"{HEADER}{TEST}", "app/quiet.py": quiet})
    git(root, "add", "-A")
    before = (root / "app/meizel.toml").read_text()
    assert carry(root, "--move-symbol", "app/test_x.py::quiet=app/quiet.py") == 0
    assert (root / "app/meizel.toml").read_text() == before
    assert "nothing to carry" in capsys.readouterr().out


def test_it_can_be_combined_with_a_renamed_target(make_repo: RepoFactory) -> None:
    root = fixture_repo(make_repo)
    move_fixture(root)
    renamed = {
        "lib/mod.py": (root / "lib/mod.py").read_text().replace("helper", "assist"),
        "app/test_x.py": (root / "app/test_x.py").read_text().replace("'helper'", "'assist'"),
        "app/calls.py": (root / "app/calls.py").read_text().replace("'helper'", "'assist'"),
    }
    write(root, renamed)
    git(root, "add", "-A")
    flags = ("--move-symbol", "app/test_x.py::fake=app/calls.py", "--rename-target")
    assert carry(root, *flags, "lib.mod.helper=lib.mod.assist") == 0
    assist = "monkeypatch.setattr lib.mod.assist"
    assert patch_rows(root, "app") == {
        f"test_x.py#{assist}": 1,
        f"calls.py#{assist}": 1,
        f"calls.py#{OTHER}": 1,
    }
    assert failures(root) == set()


def test_the_move_is_found_against_a_named_base(make_repo: RepoFactory) -> None:
    root = fixture_repo(make_repo)
    move_fixture(root)
    commit(root, "move")
    assert carry(root, "--move-symbol", "app/test_x.py::fake=app/calls.py") == 2
    assert carry(root, "--base", "HEAD~1", "--move-symbol", "app/test_x.py::fake=app/calls.py") == 0
    assert failures(root) == set()


@pytest.mark.parametrize(
    "spec",
    [
        "app/test_x.py=app/calls.py",
        "app/test_x.py::fake",
        "app/test_x.py::=app/calls.py",
        "::fake=app/calls.py",
        "app/test_x.py::fake=app/test_x.py",
        "app/test_x.py::fake.inner=app/calls.py",
        "app/test_x.py::fake=app/calls.txt",
    ],
)
def test_a_move_must_be_written_old_file_name_new_file(make_repo: RepoFactory, spec: str) -> None:
    root = fixture_repo(make_repo)
    move_fixture(root)
    assert carry(root, "--move-symbol", spec) == 2


def test_a_symbol_the_old_file_never_defined_is_an_error(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    root = fixture_repo(make_repo)
    move_fixture(root)
    assert carry(root, "--move-symbol", "app/test_x.py::ghost=app/calls.py") == 2
    assert "ghost" in capsys.readouterr().err


def test_a_symbol_missing_from_the_new_file_is_an_error(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    root = fixture_repo(make_repo)
    write(root, {"app/test_x.py": f"{HEADER}{TEST}", "app/calls.py": HEADER})
    git(root, "add", "-A")
    assert carry(root, "--move-symbol", "app/test_x.py::fake=app/calls.py") == 2
    assert "app/calls.py" in capsys.readouterr().err


def test_a_symbol_still_defined_in_the_old_file_is_a_copy_not_a_move(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    root = fixture_repo(make_repo)
    write(root, {"app/calls.py": f"{HEADER}{FIXTURE}"})
    git(root, "add", "-A")
    before = (root / "app/meizel.toml").read_text()
    assert carry(root, "--move-symbol", "app/test_x.py::fake=app/calls.py") == 2
    assert "still defines" in capsys.readouterr().err
    assert (root / "app/meizel.toml").read_text() == before


def test_the_new_file_must_exist(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    root = fixture_repo(make_repo)
    write(root, {"app/test_x.py": f"{HEADER}{TEST}"})
    assert run(root, "debt", "carry", "--move-symbol", "app/test_x.py::fake=app/calls.py") == 2
    assert "app/calls.py is not a file" in capsys.readouterr().err
