"""Lexical walking preserves definition-time inputs and excludes nested bodies."""

import ast

import pytest

from scripts.structure.imports import bindings


@pytest.mark.parametrize(
    ("source", "root_scope", "names"),
    [
        (
            "@decorate(decorator)\ndef nested(arg=default) -> annotation:\n body = hidden\n",
            False,
            ["default", "decorate", "decorator", "annotation"],
        ),
        (
            "class Nested(base, metaclass=meta):\n body = hidden\n",
            False,
            ["base", "meta"],
        ),
        (
            "value = [element for target in iterable if condition]\n",
            False,
            ["value", "iterable"],
        ),
        (
            "value = [element for target in iterable if condition for other in later]\n",
            True,
            ["target", "condition", "later", "other", "element"],
        ),
        (
            "def outer():\n local = before\n def inner(arg=default):\n  hidden = body\n after = local\n",
            True,
            ["local", "before", "default", "after", "local"],
        ),
        (
            "@((lambda arg=default: decorator_body))\ndef nested():\n hidden = body\n",
            False,
            ["decorator_body"],
        ),
    ],
)
def test_local_names_follow_scope_boundaries(
    source: str, root_scope: bool, names: list[str]
) -> None:
    tree = ast.parse(source)
    scope = tree.body[0] if root_scope else tree
    if isinstance(scope, ast.Assign):
        scope = scope.value

    found = [node.id for node in bindings.local_nodes(scope) if isinstance(node, ast.Name)]

    assert found == names


def test_deep_expressions_do_not_need_recursive_generator_frames() -> None:
    expression: ast.expr = ast.Name(id="value", ctx=ast.Load())
    for _ in range(2000):
        expression = ast.UnaryOp(op=ast.Not(), operand=expression)

    nodes = list(bindings.local_nodes(expression))

    assert len(nodes) == 4001
    assert isinstance(nodes[-2], ast.Name)
    assert nodes[-2].id == "value"
