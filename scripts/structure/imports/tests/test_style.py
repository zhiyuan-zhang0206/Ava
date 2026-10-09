"""Top-level package import style preserves the shared dependency facts."""

from __future__ import annotations

import ast

import pytest

from scripts.structure.imports import InvalidRelativeImportError, normalize, style


@pytest.mark.parametrize(
    ("rel", "text", "target"),
    [
        ("agent/graph/run.py", "import agent.execution", "agent.execution"),
        ("agent/graph/run.py", "import agent.execution as execution", "agent.execution"),
        ("agent/graph/run.py", "from agent.execution import run", "agent.execution"),
        ("agent/graph/run.py", "from agent import execution as subject", "agent"),
        ("agent/__init__.py", "from agent.graph import run", "agent.graph"),
        ("agent/graph/tests/test_run.py", "from agent.execution import run", "agent.execution"),
        ("tests/unit/test_run.py", "from tests.factories import agents", "tests.factories"),
        ("scripts/tools/run.py", "from scripts.audit import where_used", "scripts.audit"),
        ("agent/graph/run.py", "def lazy():\n    import agent.execution", "agent.execution"),
    ],
)
def test_absolute_import_within_the_top_level_package_needs_relative_syntax(
    rel: str, text: str, target: str
) -> None:
    errors = style.errors(ast.parse(text), rel)
    assert len(errors) == 1
    assert errors[0][0] == (2 if text.startswith("def lazy") else 1)
    assert f"`{target}`" in errors[0][1]
    assert "explicit relative import" in errors[0][1]


@pytest.mark.parametrize(
    ("rel", "text"),
    [
        ("agent/graph/run.py", "from ..execution import run"),
        ("agent/graph/run.py", "from .. import execution as subject"),
        ("agent/graph/deep/run.py", "from ...execution import run"),
        ("agent/__init__.py", "from .graph import run"),
        ("agent/graph/tests/test_run.py", "from ...execution import run"),
        ("tests/unit/test_run.py", "from ..factories import agents"),
        ("scripts/tools/run.py", "from ..audit import where_used"),
        ("agent/graph/run.py", "from base import agents"),
        ("agent/graph/run.py", "import ast\nimport numpy as np\nfrom typing import Protocol"),
        ("standalone.py", "import agent.graph"),
        ("agent/graph/run.py", 'EXAMPLE = "from agent.execution import run"'),
        ("agent/graph/run.py", 'import importlib\nimportlib.import_module("agent.execution")'),
    ],
)
def test_legal_syntax_and_non_statement_evidence_are_not_changed(rel: str, text: str) -> None:
    assert style.errors(ast.parse(text), rel) == []


def test_absolute_and_relative_syntax_keep_the_same_normalized_target() -> None:
    absolute = ast.parse("from agent.execution import run as call").body[0]
    relative = ast.parse("from ..execution import run as call").body[0]
    assert isinstance(absolute, ast.ImportFrom)
    assert isinstance(relative, ast.ImportFrom)
    rel = "agent/graph/run.py"
    before = normalize(absolute, rel)
    after = normalize(relative, rel)
    assert (before.base, before.bindings) == (after.base, after.bindings)
    assert style.errors(ast.Module(body=[absolute], type_ignores=[]), rel)
    assert style.errors(ast.Module(body=[relative], type_ignores=[]), rel) == []


def test_mixed_imports_report_only_internal_targets_once_per_clause() -> None:
    tree = ast.parse("import os, agent.graph, agent.execution as execute, agent.graph as graph")
    errors = style.errors(tree, "agent/work.py")
    assert len(errors) == 1
    assert errors[0][1].count("`agent.graph`") == 1
    assert "`agent.execution`" in errors[0][1]
    assert "`os`" not in errors[0][1]
    assert "preserve its bound names" in errors[0][1]


def test_multiple_imports_of_the_same_package_keep_their_source_lines() -> None:
    text = "from agent.execution import run\n\ndef lazy():\n    import agent.execution\n"
    assert [line for line, _message in style.errors(ast.parse(text), "agent/work.py")] == [1, 4]


def test_bare_imports_do_not_guess_a_loader_or_a_first_party_target() -> None:
    tree = ast.parse("import numpy\nfrom sibling import run")
    assert style.errors(tree, "agent/backends/run.py") == []


@pytest.mark.parametrize(
    ("rel", "text"),
    [
        ("agent/__init__.py", "from ..base import agents"),
        ("agent/graph/run.py", "from ...base import agents"),
        ("standalone.py", "from .agent import graph"),
        ("", "from .agent import graph"),
    ],
)
def test_relative_escape_cannot_be_accepted_as_cross_package_or_subject_free(
    rel: str, text: str
) -> None:
    with pytest.raises(InvalidRelativeImportError, match=":1:"):
        style.errors(ast.parse(text), rel)
