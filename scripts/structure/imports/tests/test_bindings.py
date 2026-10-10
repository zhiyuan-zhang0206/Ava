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


def test_query_scopes_reuse_bindings_without_crossing_lexical_parents() -> None:
    tree = ast.parse("import subprocess\ndef launch(subprocess):\n subprocess.run([])\n")
    context = bindings.module_context(tree, "probe")
    root = context.scope(tree, "probe.py")
    function = tree.body[1]
    assert isinstance(function, ast.FunctionDef)
    nested = context.scope(function, "probe.py", root)
    assert context.scope(function, "probe.py", root) is nested
    call = nested.calls[0]
    assert nested.origin(call.func) == ""
    other = bindings.Scope(ast.parse("import subprocess"), "other.py")
    assert context.scope(function, "probe.py", other) is not nested
    context.clear_scopes()
    assert context.scope(function, "probe.py", root) is not nested
    context.clear_scopes()


def test_call_index_keeps_definition_inputs_out_of_nested_bodies() -> None:
    tree = ast.parse(
        "@decorate()\ndef launch(value=default()):\n body()\n"
        "values = [element() for item in iterable() if condition()]\n"
    )
    root = bindings.Scope(tree, "probe.py")
    assert [ast.unparse(call.func) for call in root.calls] == ["default", "decorate", "iterable"]
    function = tree.body[0]
    assert isinstance(function, ast.FunctionDef)
    assert [
        ast.unparse(call.func) for call in bindings.Scope(function, "probe.py", root).calls
    ] == ["body"]


def test_binding_scan_keeps_rebinding_and_attribute_writes_opaque() -> None:
    tree = ast.parse(
        "import subprocess\nsubprocess.run = replacement\n"
        "name = 'first'\nname = 'second'\n"
        "try:\n pass\nexcept Exception as failure:\n pass\n"
    )
    scope = bindings.Scope(tree, "probe.py")
    assert scope.strings(ast.Name(id="name")) is None
    assert scope.stores["failure"] == 1
    target = ast.parse("subprocess.run", mode="eval").body
    assert scope.unmodified_origin(target) == ""


def test_binding_projection_preserves_calls_and_actual_writes_in_lexical_order() -> None:
    tree = ast.parse(
        "import subprocess\n"
        "value = source + 1\n"
        "target.attribute = receiver()\n"
        "del target.deleted\n"
        "@decorate()\n"
        "def launch(arg=default()):\n hidden = body()\n"
        "result = [item() for name in iterable() if check()]\n"
    )
    projected = list(bindings.local_nodes(tree, bindings_only=True))
    assert [ast.unparse(n.func) for n in projected if isinstance(n, ast.Call)] == [
        "receiver",
        "default",
        "decorate",
        "iterable",
    ]
    assert [n.id for n in projected if isinstance(n, ast.Name)] == ["value", "result"]
    assert [n.attr for n in projected if isinstance(n, ast.Attribute)] == ["attribute", "deleted"]
    assert not any(isinstance(n, ast.Constant | ast.expr_context | ast.operator) for n in projected)
    scope = bindings.Scope(tree, "probe.py")
    assert scope.stores == {"subprocess": 1, "value": 1, "launch": 1, "result": 1}
    assert [n.attr for n in scope.attribute_writes] == ["attribute", "deleted"]
    assert [ast.unparse(n.func) for n in scope.calls] == [
        "receiver",
        "default",
        "decorate",
        "iterable",
    ]


def test_binding_projection_keeps_writes_in_unselected_expression_children() -> None:
    tree = ast.parse("result = sink((extra := read()))\n")
    scope = bindings.Scope(tree, "probe.py")
    assert scope.stores == {"result": 1, "extra": 1}
    assert [ast.unparse(n.func) for n in scope.calls] == ["sink", "read"]
