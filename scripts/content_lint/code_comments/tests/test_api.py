import ast
from pathlib import Path

import pytest

from scripts.content_lint.code_comments.check import check
from scripts.content_lint.code_comments.render import WithoutDocstrings, render, render_source
from scripts.content_lint.code_comments.tests.cases import record, repository


def api_files() -> dict[str, str]:
    return {
        "ava/__init__.py": "from . import files\n__all_for_ava__ = ['files']\n",
        "ava/files.py": "def read(path):\n    return path\n__all_for_ava__ = ['read']\nraise RuntimeError('Never import application code')\n",
        "ava/docs/files.ava.okf.md": record(
            id="read-contract",
            kind="api",
            statement="Read a file.",
            code=["ava/files.py:read"],
            tests=["ava/tests/test_files.py::test_read"],
            api="ava.files.read",
        ),
        "ava/tests/test_files.py": "def test_read():\n    assert True\n",
    }


def test_generated_docstrings_keep_the_existing_help_input_and_are_checked(tmp_path: Path) -> None:
    repo = repository(tmp_path, api_files())
    assert any(item.code == "contract-binding" for item in check(repo).findings)
    assert render(repo) == ["ava/files.py"]
    assert not check(repo, strict_paths=repo.files).findings
    tree = ast.parse(repo.text("ava/files.py"))
    first = tree.body[0]
    assert isinstance(first, ast.FunctionDef)
    assert ast.get_docstring(first) == "Read a file."
    assert render(repo) == []


def test_a_private_function_cannot_be_declared_api_to_keep_its_notes(tmp_path: Path) -> None:
    source = api_files()
    source["ava/files.py"] = "def read(path): return path\n__all_for_ava__ = []\n"
    repo = repository(tmp_path, source)
    result = check(repo)
    assert any("cannot be proved" in item.message for item in result.findings)
    with pytest.raises(ValueError, match="cannot be proved"):
        render(repo)


@pytest.mark.parametrize(
    "source",
    [
        "def read(path): return path\n",
        "def read(path):\n    return path\n",
        "def read(path):\n    'Old text'\n    return path\n",
        "def read(path):\n    name = 'caf\u00e9'\n    return name + path\n",
    ],
)
def test_generation_preserves_the_executable_body(source: str) -> None:
    rendered = render_source(source, {"read": "Read a file.\nPreserve its contents."})
    before = ast.parse(source).body[0]
    after = ast.parse(rendered).body[0]
    assert isinstance(before, ast.FunctionDef) and isinstance(after, ast.FunctionDef)
    assert ast.get_docstring(after) == "Read a file.\nPreserve its contents."
    before_body = before.body[1:] if ast.get_docstring(before) else before.body
    assert [ast.dump(node) for node in before_body] == [ast.dump(node) for node in after.body[1:]]


@pytest.mark.parametrize("declaration", ["LIMIT: int = 3", "LIMIT = 3"])
def test_exported_attribute_help_is_preserved_and_regenerated(
    tmp_path: Path, declaration: str
) -> None:
    source = api_files()
    source["ava/files.py"] = declaration + '\n"Old help."\n__all_for_ava__ = ["LIMIT"]\n'
    source["ava/docs/files.ava.okf.md"] = record(
        id="limit-contract",
        kind="api",
        statement="Maximum request count.",
        code=["ava/files.py:LIMIT"],
        tests=["ava/tests/test_files.py::test_read"],
        api="ava.files.LIMIT",
    )
    repo = repository(tmp_path, source)
    assert render(repo) == ["ava/files.py"]
    assert not check(repo, strict_paths=repo.files).findings
    assert "Maximum request count." in repo.text("ava/files.py")
    assert declaration in repo.text("ava/files.py")
    assert render(repo) == []


def test_attribute_generation_without_existing_help_keeps_the_assignment() -> None:
    source = "LIMIT: int = 3"
    output = render_source(source, {"LIMIT": "Maximum request count."})
    assert output.startswith(source + "\n")
    expression = ast.parse(output).body[1]
    assert isinstance(expression, ast.Expr)
    assert ast.literal_eval(expression.value) == "Maximum request count."


def test_unproved_dynamic_attribute_fails_without_deleting_help(tmp_path: Path) -> None:
    source = api_files()
    source["ava/files.py"] = 'LIMIT: int\n"Keep this dynamic help."\n__all_for_ava__ = ["LIMIT"]\n'
    source["ava/docs/files.ava.okf.md"] = record(
        id="limit-contract",
        kind="api",
        statement="Maximum request count.",
        code=["ava/files.py:LIMIT"],
        tests=["ava/tests/test_files.py::test_read"],
        api="ava.files.LIMIT",
    )
    repo = repository(tmp_path, source)
    with pytest.raises(ValueError, match="cannot be proved"):
        render(repo)
    assert repo.text("ava/files.py") == source["ava/files.py"]


def property_files() -> dict[str, str]:
    source = api_files()
    source["ava/files.py"] = "\n".join(
        [
            "import sys",
            "from types import ModuleType",
            "__all_for_ava__ = ['context']",
            "context: object",
            '"Original context help."',
            "class Surface(ModuleType):",
            "    @property",
            "    def context(self): return obtain_context()",
            "    @context.setter",
            "    def context(self, value): bind_context(value)",
            "    @context.deleter",
            "    def context(self): unbind_context()",
            "sys.modules[__name__].__class__ = Surface",
            "raise RuntimeError('Never import application source')",
            "",
        ]
    )
    source["ava/docs/files.ava.okf.md"] = record(
        id="context-help",
        kind="api",
        statement="Your execution context.",
        code=["ava/files.py:context"],
        tests=["ava/tests/test_files.py::test_read"],
        api="ava.files.context",
    )
    return source


def test_module_property_help_is_projected_at_the_annotation(tmp_path: Path) -> None:
    source = property_files()
    repo = repository(tmp_path, source)
    assert repo.source("ava/files.py").symbols["Surface.context"].ambiguous
    assert any(item.code == "contract-binding" for item in check(repo).findings)
    assert render(repo) == ["ava/files.py"]
    assert not check(repo, strict_paths=repo.files).findings
    after = repo.text("ava/files.py")
    assert "context: object\n'Your execution context.'\nclass Surface" in after
    before_tree = WithoutDocstrings().visit(ast.parse(source["ava/files.py"]))
    after_tree = WithoutDocstrings().visit(ast.parse(after))
    assert ast.dump(before_tree) == ast.dump(after_tree)
    assert render(repo) == []


@pytest.mark.parametrize(
    "old,new",
    [
        ("context: object", "context: object\ncontext: object"),
        ("sys.modules[__name__].__class__ = Surface", ""),
        ("    @context.deleter", "    context = replacement\n    @context.deleter"),
    ],
)
def test_unproved_property_help_remains_untouched(tmp_path: Path, old: str, new: str) -> None:
    source = property_files()
    source["ava/files.py"] = source["ava/files.py"].replace(old, new)
    repo = repository(tmp_path, source)
    with pytest.raises(ValueError):
        render(repo)
    assert repo.text("ava/files.py") == source["ava/files.py"]
