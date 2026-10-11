"""Restricted Python: dynamic forms are errors unless the file declares its loads."""

from __future__ import annotations

import pytest

from scripts.meizel.tests.synthetic import RepoFactory, diagnostics, verdict

LIB = {"lib/__init__.py": "", "lib/m.py": ""}


def restricted(make_repo: RepoFactory, source: str) -> list[int]:
    root = make_repo({**LIB, "app/run.py": source})
    return [d.line for d in diagnostics(root, "restricted-python")]


@pytest.mark.parametrize(
    "source",
    [
        "import importlib\nimportlib.import_module('lib.m')\n",
        "from importlib import import_module\nimport_module('lib.m')\n",
        "from importlib import import_module as im\nim('lib.m')\n",
        "import importlib as il\nil.import_module('lib.m')\n",
        "import importlib.util as iu\niu.spec_from_file_location('m', 'lib/m.py')\n",
        "import importlib.util\nimportlib.util.spec_from_file_location('m', 'lib/m.py')\n",
        "from importlib.util import spec_from_file_location as sfl\nsfl('m', 'x')\n",
        "import runpy\nrunpy.run_path('x.py')\n",
        "from runpy import run_module as go\ngo('lib.m')\n",
        "__import__('lib.m')\n",
        "import builtins\nbuiltins.__import__('lib.m')\n",
        "exec('x = 1')\n",
        "code = compile('x = 1', 'f', 'exec')\n",
        "import importlib\nload = importlib.import_module\n",
    ],
)
def test_dynamic_forms_are_found_through_bindings(make_repo: RepoFactory, source: str) -> None:
    assert restricted(make_repo, source), source


@pytest.mark.parametrize(
    "source",
    [
        "import re\nre.compile('x')\n",
        "from re import compile\ncompile('x')\n",
        "def compile(x): ...\ncompile(1)\n",
        "import importlib\nimportlib.reload\n",
        "class Box:\n    def exec(self): ...\nBox().exec()\n",
        "import importlib\n\ndef f(importlib):\n    return importlib.import_module\n",
    ],
)
def test_lookalikes_are_not_restricted(make_repo: RepoFactory, source: str) -> None:
    assert restricted(make_repo, source) == []


def test_python_dash_c_with_first_party_imports_is_restricted(make_repo: RepoFactory) -> None:
    source = (
        "import subprocess, sys\n"
        "subprocess.run([sys.executable, '-c', 'import lib.m'])\n"
        "code = 'from lib import m; print(m)'\n"
        "subprocess.run([sys.executable, '-B', '-c', code])\n"
        "subprocess.run('python3 -c \"import lib\"', shell=True)\n"
    )
    assert restricted(make_repo, source) == [2, 4, 5]


def test_python_dash_c_without_first_party_imports_is_fine(make_repo: RepoFactory) -> None:
    source = (
        "import subprocess, sys\n"
        "subprocess.run([sys.executable, '-c', 'import os; print(os.getcwd())'])\n"
        "subprocess.run(['git', '-c', 'user.name=x', 'status'])\n"
        "subprocess.run(['sh', '-c', 'import lib'])\n"
        "subprocess.run([sys.executable, '-c', f'print({1+1})'])\n"
    )
    assert restricted(make_repo, source) == []


def test_python_dash_c_with_an_unreadable_source_cannot_be_shown_static(
    make_repo: RepoFactory,
) -> None:
    source = "import subprocess, sys\n\ndef run(code):\n    subprocess.run([sys.executable, '-c', code])\n"
    assert restricted(make_repo, source) == [4]


@pytest.mark.parametrize(
    "source",
    [
        "from unittest import mock\nmock.patch('lib.m.x')\n",
        "from unittest.mock import patch\n\n@patch('lib.m.x')\ndef test_a(m): ...\n",
        "import unittest.mock\nunittest.mock.patch.dict('os.environ', {})\n",
        "from unittest.mock import patch\npatch.object('lib.m', 'x')\n",
        "def test_a(monkeypatch: pytest.MonkeyPatch):\n    monkeypatch.setattr('lib.m.x', 1)\n",
        "def test_a(monkeypatch: pytest.MonkeyPatch):\n    monkeypatch.delattr('lib.m.x')\n",
    ],
)
def test_string_patch_targets_are_restricted(make_repo: RepoFactory, source: str) -> None:
    assert restricted(make_repo, source), source


def test_a_declared_load_exempts_the_file_and_becomes_an_edge(make_repo: RepoFactory) -> None:
    files = {
        **LIB,
        "app/__init__.py": "",
        "app/loader.py": "import importlib\nimportlib.import_module('lib.m')\n",
        "app/meizel.toml": '[loads]\n"loader.py" = ["lib"]\n',
        "meizel.toml": '[uses]\n"app" = ["lib"]\n',
    }
    assert verdict(make_repo(files)) == set()


def test_a_declared_load_is_judged_like_any_other_edge(make_repo: RepoFactory) -> None:
    files = {
        **LIB,
        "app/__init__.py": "",
        "app/loader.py": "import importlib\nimportlib.import_module('lib.m')\n",
        "app/meizel.toml": '[loads]\n"loader.py" = ["lib"]\n',
    }
    [found] = diagnostics(make_repo(files), "undeclared-edge")
    assert "app/loader.py -> lib" in found.message
    assert (found.path, found.line) == ("app/meizel.toml", 2)


def test_a_declared_load_with_no_dynamic_form_is_stale(make_repo: RepoFactory) -> None:
    files = {
        **LIB,
        "app/__init__.py": "",
        "app/loader.py": "x = 1\n",
        "app/meizel.toml": '[loads]\n"loader.py" = ["lib"]\n',
    }
    assert ("stale-load", "app/meizel.toml") in verdict(make_repo(files))


def test_loads_must_name_python_files_and_tracked_directories(make_repo: RepoFactory) -> None:
    files = {
        **LIB,
        "app/__init__.py": "",
        "app/loader.py": "import importlib\nimportlib.import_module('lib.m')\n",
        "app/meizel.toml": '[loads]\n"loader.py" = ["nowhere"]\n"__init__.py" = []\n"missing.py" = ["lib"]\n',
    }
    found = diagnostics(make_repo(files), "manifest-unknown-child")
    assert len(found) == 2


def test_a_non_literal_pytest_plugins_is_restricted(make_repo: RepoFactory) -> None:
    files = {**LIB, "conftest.py": "names = ['lib.m']\npytest_plugins = names\n"}
    assert [d.line for d in diagnostics(make_repo(files), "restricted-python")] == [2]
