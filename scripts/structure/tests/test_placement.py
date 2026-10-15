from __future__ import annotations

import ast
import pathlib

import pytest

from scripts.structure import imports, placement
from scripts.structure.tests.patch_repo import make_repo, write


@pytest.fixture
def root(tmp_path: pathlib.Path) -> pathlib.Path:
    return make_repo(tmp_path)


@pytest.mark.parametrize(
    ("rel", "text", "module", "name"),
    [
        (
            "base/net/tests/test_relative.py",
            "from ..retry import backoff as call",
            "base.net.retry",
            "call",
        ),
        (
            "base/net/tests/test_relative.py",
            "from .. import retry as subject",
            "base.net.retry",
            "subject",
        ),
        (
            "base/net/tests/nested/test_relative.py",
            "from ...retry import backoff",
            "base.net.retry",
            "backoff",
        ),
        ("base/net/__init__.py", "from .retry import backoff as call", "base.net.retry", "call"),
        (
            "base/net/tests/test_relative.py",
            "from ...db import pool as database",
            "base.db.pool",
            "database",
        ),
        ("base/net/tests/test_relative.py", "from .. import *", "base.net", "*"),
        (
            "base/net/tests/test_relative.py",
            "def run():\n    from ..retry import backoff as call",
            "base.net.retry",
            "call",
        ),
    ],
)
def test_relative_imports_resolve_module_members_and_local_aliases(
    root: pathlib.Path, rel: str, text: str, module: str, name: str
) -> None:
    refs = placement.collect_references(ast.parse(text), placement.ModuleIndex(root), rel)
    assert [(ref.module, ref.names) for ref in refs] == [(module, (name,))]


def test_import_bindings_match_python_module_and_member_aliases(root: pathlib.Path) -> None:
    text = (
        "import base.net.retry\nimport base.net.retry as module\n"
        "from base.net import retry as subject\n"
        "from base.net.retry import backoff as call\n"
    )
    clauses = [
        imports.normalize(node, "cli/commands/run.py")
        for node in ast.parse(text).body
        if isinstance(node, ast.Import | ast.ImportFrom)
    ]
    assert [clause.origins for clause in clauses] == [
        {"base": "base"},
        {"module": "base.net.retry"},
        {"subject": "base.net.retry"},
        {"call": "base.net.retry.backoff"},
    ]
    refs = placement.collect_references(ast.parse(text), placement.ModuleIndex(root))
    assert [ref.module for ref in refs] == ["base.net.retry"] * 4


def test_reexported_member_keeps_its_import_door_as_dependency(root: pathlib.Path) -> None:
    write(root, "base/net/__init__.py", "from .retry import backoff\n")
    tree = ast.parse("from base.net import backoff as call\n")
    refs = placement.collect_references(tree, placement.ModuleIndex(root), "cli/commands/run.py")
    assert [(ref.module, ref.names) for ref in refs] == [("base.net", ("call",))]


def test_invalid_source_sample_is_not_an_executed_import(root: pathlib.Path) -> None:
    text = 'SAMPLE = "from ..base.net import retry"\n'
    assert placement.collect_references(ast.parse(text), placement.ModuleIndex(root)) == []
