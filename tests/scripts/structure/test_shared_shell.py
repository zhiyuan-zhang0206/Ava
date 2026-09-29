"""Rule 7: the `shared/` release-probe shell stays three files that nothing imports.

The shell exists for the release probes that run inside another version's image
(shared/__init__.py); the last two tests prove this image answers them.
"""

from __future__ import annotations

import ast
import json
import pathlib
import subprocess
import sys

import pytest

from base.deploy.release import runtime_prepare
from base.runtime_abi import current_abi
from scripts.structure import shared_shell

_REPO = pathlib.Path(__file__).resolve().parents[3]


def _write(root: pathlib.Path, name: str, text: str = "") -> None:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_the_shell_directory_admits_only_the_whitelisted_files(tmp_path: pathlib.Path) -> None:
    assert shared_shell.shell_errors(tmp_path) == []
    for name in shared_shell.SHELL_FILES:
        _write(tmp_path, name)
    _write(tmp_path, "shared/__pycache__/runtime_abi.cpython-312.pyc")
    assert shared_shell.shell_errors(tmp_path) == []

    _write(tmp_path, "shared/config.py", "from base.config import settings\n")
    errors = shared_shell.shell_errors(tmp_path)
    assert len(errors) == 1
    assert errors[0].startswith("shared/config.py: not a release-probe shell file")


@pytest.mark.parametrize(
    "rel,source",
    [
        ("gateway/a.py", "import shared.runtime_abi\n"),
        ("gateway/a.py", "from shared import runtime_abi\n"),
        ("ops/b.py", "import importlib\nimportlib.import_module('shared')\n"),
        ("cli/c.py", "__import__('shared')\n"),
        ("tests/base/test_x.py", "from shared.runtime_plugins import verify_plugin_dependencies\n"),
    ],
)
def test_an_import_of_the_shell_fails_anywhere(rel: str, source: str) -> None:
    errors = shared_shell.usage_errors([(rel, source)])
    assert any("imports the `shared` release-probe shell" in error for error in errors), errors
    assert all(error.startswith(f"{rel}:") for error in errors)


def test_string_mentions_outside_tests_need_an_allowlist_entry() -> None:
    code = (
        '"""Docstrings about shared.config are prose."""\n'
        'TARGET = "shared.config.settings"\n'
        'PROSE = "the shared data plane"\n'
        'CHAIN = "daemon.shared.db"\n'
        'MEMBER = "shared/release-build.json"\n'
    )
    errors = shared_shell.usage_errors([("gateway/a.py", code), ("tests/test_a.py", code)])
    assert errors == [
        "gateway/a.py:2: names the `shared` release-probe shell (`shared.config.settings`); "
        "only the sites in scripts/structure/shared_shell.py ALLOWED_MENTIONS may",
        "gateway/a.py:5: names the `shared` release-probe shell (`shared/release-build.json`); "
        "only the sites in scripts/structure/shared_shell.py ALLOWED_MENTIONS may",
    ]


@pytest.mark.parametrize("count,expected", [(0, 1), (1, 0), (2, 1)])
def test_allowlisted_mentions_are_counted_exactly(
    monkeypatch: pytest.MonkeyPatch, count: int, expected: int
) -> None:
    monkeypatch.setattr(
        shared_shell, "ALLOWED_MENTIONS", {("base/probe.py", "shared.runtime_abi"): 1}
    )
    source = "".join(f'P{i} = "import shared.runtime_abi"\n' for i in range(count)) or "x = 1\n"

    errors = shared_shell.usage_errors([("base/probe.py", source)])

    assert len(errors) == expected, errors
    if expected:
        assert f"expects `shared.runtime_abi` 1x, found {count}x" in errors[0]
    # An entry whose file is not among the sources is not judged.
    assert shared_shell.usage_errors([("base/other.py", "x = 1\n")]) == []


def test_files_pinned_to_another_revision_are_exempt() -> None:
    source = "from shared.migrations import apply_pending_migrations\n"
    assert shared_shell.usage_errors([("scripts/legacy_lkg/cold_boot.py", source)]) == []


def test_the_repository_obeys_rule_7() -> None:
    assert shared_shell.repository_errors(_REPO) == []


def _probe(code: str, *argv: str, cwd: pathlib.Path) -> str:
    """Run `code` the way preparation runs a probe: isolated, in the image's interpreter."""
    result = subprocess.run(  # noqa: S603 - this interpreter, a fixed probe
        [sys.executable, "-I", "-B", "-c", code, *argv],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_this_image_answers_the_abi_probe(tmp_path: pathlib.Path) -> None:
    answered = json.loads(_probe(runtime_prepare.ABI_PROBE, cwd=tmp_path))
    assert answered == current_abi().to_json()


def test_this_image_answers_the_plugin_probe_imports(tmp_path: pathlib.Path) -> None:
    """Every import the plugin probe makes through the shell resolves to `base`'s object."""
    imports = [
        node
        for node in ast.walk(ast.parse(runtime_prepare.PLUGIN_PROBE))
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("shared")
    ]
    assert [node.module for node in imports] == ["shared.runtime_plugins"]
    checks = [
        f"from {node.module} import {alias.name} as probed; "
        f"import {node.module.replace('shared', 'base', 1)} as real; "
        f"assert probed is real.{alias.name}"
        for node in imports
        if node.module
        for alias in node.names
    ]
    for check in checks:
        _probe(check, cwd=tmp_path)
