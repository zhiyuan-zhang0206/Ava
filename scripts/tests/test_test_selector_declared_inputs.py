"""What the selector does with inputs the analysis cannot bound, and with declarations of them.

A load it cannot resolve can run any code, so a test that reaches one always runs. A policy
`[loads]` entry turns the load into a dependency on the named directories and an `[external]`
entry says the loaded content is not part of the repository; an unanchored file read can only
matter to a change that includes a data file; an existence probe loads nothing.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.ci import test_impact, test_selector


def _write(root: Path, name: str, text: str = "") -> None:
    file = root / name
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(text)


def _repo(root: Path) -> Path:
    """Two consumers of one changed module, a filler that keeps subsets under the 80% guard,
    and a helper whose behavior each test case configures."""
    _write(
        root,
        "pyproject.toml",
        '[tool.pytest.ini_options]\ntestpaths = ["tests", "base/**/tests"]\n',
    )
    _write(root, "base/unrelated/tests/test_filler.py", "def test_filler(): pass\n")
    _write(root, "base/tests/test_own.py", "def test_own(): pass\n")  # owns base/leaf.py
    _write(root, "base/leaf.py")
    _write(root, "base/data.json", "{}\n")
    _write(root, "tests/consumer/test_leaf.py", "from base import leaf\n")
    _write(root, "tests/consumer/test_helper.py", "from tests.support import helper\n")
    _write(
        root,
        ".test_durations",
        json.dumps(
            {
                "base/unrelated/tests/test_filler.py::test_filler": 1000,
                "base/tests/test_own.py::test_own": 1,
                "tests/consumer/test_leaf.py::test_x": 1,
                "tests/consumer/test_helper.py::test_x": 1,
            }
        ),
    )
    return root


def _select(root: Path, *changed: str) -> test_selector.SelectionResult:
    return test_selector.select_tests(list(changed), repo_root=root)


def test_an_unanchored_read_blocks_only_a_change_that_includes_a_data_file(
    tmp_path: Path,
) -> None:
    root = _repo(tmp_path)
    _write(root, "tests/support/helper.py", "def load(path):\n    return path.read_text()\n")

    python_only = _select(root, "base/leaf.py")
    with_data = _select(root, "base/data.json")

    assert python_only.decision == "SELECTED", python_only.as_json()
    assert "tests/consumer/test_helper.py" not in python_only.tests
    assert "tests/consumer/test_leaf.py" in python_only.tests
    assert with_data.decision == "SELECTED", with_data.as_json()
    assert "tests/consumer/test_helper.py" in with_data.tests
    assert any("tests/support/helper.py" in item for item in with_data.diagnostics)
    assert python_only.diagnostics == ()


def test_a_load_that_cannot_be_resolved_always_blocks(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _write(root, "tests/support/helper.py", "import importlib\nimportlib.import_module(name)\n")

    result = _select(root, "base/leaf.py")

    assert result.decision == "SELECTED", result.as_json()
    assert "tests/consumer/test_helper.py" in result.tests
    assert any("tests/support/helper.py" in item for item in result.diagnostics)


def test_a_declared_load_depends_on_its_directories_instead_of_blocking(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _write(root, "base/plugins/one.py")
    _write(root, "tests/support/helper.py", "import importlib\nimportlib.import_module(name)\n")
    _write(root, "meizel.policy.toml", '[loads]\n"tests/support/helper.py" = ["base/plugins"]\n')

    unrelated = _select(root, "base/leaf.py")
    plugin = _select(root, "base/plugins/one.py")

    assert "tests/consumer/test_helper.py" not in unrelated.tests
    assert "tests/consumer/test_leaf.py" in unrelated.tests
    assert unrelated.diagnostics == ()
    assert "tests/consumer/test_helper.py" in plugin.tests


def test_external_content_is_declared_for_sources_but_not_for_tests(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _write(root, "tests/support/helper.py", "from base import discovery\n")
    _write(root, "base/discovery.py", "import importlib\nimportlib.import_module(name)\n")
    _write(root, "tests/consumer/test_ext.py", "import importlib\nimportlib.import_module(name)\n")
    _write(
        root,
        "meizel.policy.toml",
        '[external]\n"base/discovery.py" = "loads discovered plugins"\n'
        '"tests/consumer/test_ext.py" = "loads a script by path"\n',
    )

    result = _select(root, "base/leaf.py")

    assert result.decision == "SELECTED", result.as_json()
    # The source's declaration keeps its consumer out; a listed test may load a repository file by
    # path, so it keeps running.
    assert "tests/consumer/test_helper.py" not in result.tests
    assert "tests/consumer/test_ext.py" in result.tests


def test_a_declaration_does_not_cover_an_unanchored_read(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _write(root, "tests/support/helper.py", "def load(path):\n    return path.read_text()\n")
    _write(root, "meizel.policy.toml", '[external]\n"tests/support/helper.py" = "x"\n')
    # [external] is for sources; listing the helper changes nothing about its read of a data file.
    assert "tests/consumer/test_helper.py" in _select(root, "base/data.json").tests
    _write(root, "meizel.policy.toml", '[loads]\n"tests/support/helper.py" = ["base"]\n')
    assert "tests/consumer/test_helper.py" in _select(root, "base/data.json").tests


def test_an_existence_probe_loads_nothing(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _write(
        root,
        "tests/support/helper.py",
        "import importlib.util\n\ndef exists(name):\n    return importlib.util.find_spec(name)\n",
    )

    impact = test_impact.build_impact(root, test_selector.load_checkout(root).collectable)
    result = _select(root, "base/leaf.py")

    assert impact.unknown == ()
    assert "tests/consumer/test_helper.py" not in result.tests


def test_an_invalid_policy_fails_fast(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _write(root, "meizel.policy.toml", "[external]\nnot a valid line\n")

    with pytest.raises(ValueError, match=r"meizel\.policy\.toml is invalid"):
        _select(root, "base/leaf.py")
