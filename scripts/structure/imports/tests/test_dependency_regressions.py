"""Dependency evidence must not silently certify incomplete module subjects."""

import ast
from pathlib import Path

from scripts.structure import locality, placement
from scripts.structure.tests.patch_repo import make_repo


def test_dynamic_top_level_module_is_evidence(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    evidence = placement.collect_reference_evidence(
        ast.parse('import importlib\nimportlib.import_module("ava")'),
        placement.ModuleIndex(root),
        "scripts/tests/test_probe.py",
    )
    assert [ref.module for ref in evidence.refs] == ["ava"]
    assert evidence.unresolved == []


def test_unknown_dynamic_target_retains_incomplete_evidence(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    evidence = placement.collect_reference_evidence(
        ast.parse("import importlib\ndef load(name):\n return importlib.import_module(name)"),
        placement.ModuleIndex(root),
        "scripts/tests/test_probe.py",
    )
    assert len(evidence.unresolved) == 1
    assert evidence.unresolved[0].line == 3


def test_missing_absolute_module_does_not_become_its_parent(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    evidence = placement.collect_reference_evidence(
        ast.parse("import ava.nonexistent_module"),
        placement.ModuleIndex(root),
        "scripts/tests/test_probe.py",
    )
    assert evidence.refs == []
    assert len(evidence.unresolved) == 1


def test_unrelated_parameter_does_not_hide_global_private_reach(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    tree = ast.parse(
        "import base.net.retry as subject\nsubject._sleep(1)\n"
        "def unrelated(subject):\n return subject\n"
    )
    assert locality.private_imports(tree, "cli/probe.py", ("base", "cli"), root) == {
        "cli/probe.py::base.net.retry._sleep": [2]
    }


def test_comprehension_target_does_not_hide_global_private_reach(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    tree = ast.parse(
        "import base.net.retry as subject\n[subject for subject in []]\nsubject._sleep(1)\n"
    )
    assert locality.private_imports(tree, "cli/probe.py", ("base", "cli"), root) == {
        "cli/probe.py::base.net.retry._sleep": [3]
    }
