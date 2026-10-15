from __future__ import annotations

import pytest

from meizel.tests.synthetic import RepoFactory, run

TWO_BAD = {
    "pkg/__init__.py": "",
    "pkg/a.py": "from pkg import b\n",
    "pkg/b.py": "VALUE = 1\n",
    "pkg/c.py": "from pkg import b\n",
}


def test_only_the_changed_files_are_judged(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    root = make_repo(TWO_BAD)
    assert run(root, "check") == 1
    assert "pkg/c.py" in capsys.readouterr().out
    assert run(root, "check", "--changed", "pkg/a.py") == 1
    out = capsys.readouterr().out
    assert "pkg/a.py:1" in out
    assert "pkg/c.py" not in out
    assert run(root, "check", "--changed", "pkg/b.py") == 0
    assert "meizel: ok" in capsys.readouterr().out


def test_non_python_and_deleted_paths_are_ignored(make_repo: RepoFactory) -> None:
    root = make_repo(
        {**TWO_BAD, "pkg/meizel.toml": '[uses]\n"a.py" = ["b.py"]\n"c.py" = ["b.py"]\n'}
    )
    assert run(root, "check", "--changed", "README.md", "pkg/gone.py", "pkg/a.py") == 0


def test_a_broken_declared_graph_fails_whatever_changed(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    manifest = '[uses]\n"a.py" = ["b.py"]\n"b.py" = ["a.py"]\n'
    root = make_repo({**TWO_BAD, "pkg/meizel.toml": manifest})
    assert run(root, "check", "--changed", "pkg/c.py") == 1
    assert "manifest-cycle" in capsys.readouterr().out


def test_the_stale_checks_belong_to_the_full_check(make_repo: RepoFactory) -> None:
    manifest = (
        '[uses]\n"a.py" = ["b.py"]\n"c.py" = ["b.py"]\n"b.py" = []\n"__init__.py" = ["a.py"]\n'
    )
    root = make_repo({**TWO_BAD, "pkg/meizel.toml": manifest})
    assert run(root, "check") == 1
    assert run(root, "check", "--changed", "pkg/a.py", "pkg/c.py") == 0


def test_restricted_forms_are_judged_in_changed_files_only(make_repo: RepoFactory) -> None:
    files = {"app/__init__.py": "", "app/x.py": "exec('1')\n", "app/y.py": "exec('2')\n"}
    root = make_repo(files)
    assert run(root, "check", "--changed", "app/x.py") == 1
    assert run(root, "check", "--changed", "app/__init__.py") == 0


def test_fixture_requests_resolve_through_unchanged_conftests(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    files = {
        "t/__init__.py": "",
        "t/conftest.py": 'pytest_plugins = ["t.helpers"]\n',
        "t/helpers.py": "import pytest\n\n@pytest.fixture\ndef thing():\n    return 1\n",
        "t/test_x.py": "def test_x(thing):\n    assert thing\n",
        "t/test_y.py": "def test_y(thing):\n    assert thing\n",
    }
    root = make_repo(files)
    assert run(root, "check", "--changed", "t/test_x.py") == 1
    out = capsys.readouterr().out
    assert "t/test_x.py:1: [undeclared-edge] t/test_x.py -> t/helpers.py (fixture thing)" in out
    assert "t/test_y.py" not in out


def test_recorded_debt_of_a_changed_file_is_reconciled_exactly(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    files = {
        "pkg/__init__.py": "",
        "pkg/a.py": "from pkg.b import _secret\n",
        "pkg/b.py": "_secret = 1\n",
        "pkg/d.py": "VALUE = 1\n",
        "pkg/meizel.toml": (
            '[uses]\n"a.py" = ["b.py"]\n\n[debt.private-name]\n'
            '"a.py -> b.py#_secret" = 1\n"d.py -> b.py#_secret" = 1\n'
        ),
    }
    root = make_repo(files)
    assert run(root, "check", "--changed", "pkg/b.py") == 0, (
        "debt of an unchanged file is not judged"
    )
    assert run(root, "check", "--changed", "pkg/a.py") == 0
    assert run(root, "check", "--changed", "pkg/d.py") == 1
    assert "debt-paid" in capsys.readouterr().out
    assert run(root, "check", "--changed", "pkg/gone.py") == 0


def test_a_changed_file_without_recorded_debt_fails_in_a_strict_to_be_directory(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    files = {
        "pkg/__init__.py": "",
        "pkg/a.py": "from pkg.b import _secret\n",
        "pkg/b.py": "_secret = 1\n",
        "pkg/meizel.toml": '[uses]\n"a.py" = ["b.py"]\n\n[debt.private-name]\n"x.py -> b.py#_s" = 1\n',
    }
    root = make_repo(files)
    assert run(root, "check", "--changed", "pkg/a.py") == 1
    out = capsys.readouterr().out
    assert "pkg is strict-to-be" in out


def test_changed_cannot_be_combined_with_paths(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    root = make_repo(TWO_BAD)
    assert run(root, "check", "pkg", "--changed", "pkg/a.py") == 2
    assert "--changed" in capsys.readouterr().err


def test_a_deleted_file_with_recorded_debt_is_paid(make_repo: RepoFactory) -> None:
    files = {
        "pkg/__init__.py": "",
        "pkg/b.py": "_secret = 1\n",
        "pkg/meizel.toml": '[debt.private-name]\n"gone.py -> b.py#_secret" = 1\n',
    }
    root = make_repo(files)
    assert run(root, "check", "--changed", "pkg/gone.py") == 1


def test_a_changed_loads_declaration_is_judged(make_repo: RepoFactory) -> None:
    files = {
        "lib/__init__.py": "",
        "app/__init__.py": "",
        "app/loader.py": "import importlib\nimportlib.import_module('lib')\n",
        "app/meizel.toml": '[loads]\n"loader.py" = ["lib"]\n',
    }
    root = make_repo(files)
    assert run(root, "check", "--changed", "app/__init__.py") == 1
    assert run(root, "check") == 1


def test_debt_about_a_loader_that_did_not_change_is_not_judged(make_repo: RepoFactory) -> None:
    files = {
        "lib/__init__.py": "",
        "app/__init__.py": "",
        "app/loader.py": "import importlib\nimportlib.import_module('lib')\n",
        "app/other.py": "VALUE = 1\n",
        "app/meizel.toml": (
            '[loads]\n"loader.py" = ["lib"]\n\n[debt.private-name]\n"loader.py -> other.py#_n" = 1\n'
        ),
        "meizel.toml": '[uses]\n"app" = ["lib"]\n',
    }
    root = make_repo(files)
    assert run(root, "check", "--changed", "app/other.py") == 0
    assert run(root, "check", "--changed", "app/loader.py") == 1
