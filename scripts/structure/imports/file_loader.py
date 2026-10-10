"""Pure bounded proofs for local spec-to-module-to-execution chains."""

from __future__ import annotations

import ast
from collections.abc import Iterator, Set
from dataclasses import dataclass
from pathlib import Path

from . import bindings

__all__ = ["ExecutionProof", "input_domain", "prior_calls", "prove_execution"]


@dataclass(frozen=True)
class ExecutionProof:
    """A local execution witness, or the reason that the recognized chain is opaque.

    A complete witness retains the original factory and its plain-bound source
    expression. The collector owns path anchoring, source reads and analysis.
    """

    spec: ast.Call | None
    source: ast.expr | None
    reason: str | None


def _object_value(node: ast.expr, scope: bindings.Scope) -> ast.expr:
    seen: set[int] = set()
    while id(node) not in seen:
        seen.add(id(node))
        value = scope.value(node)
        if value is node:
            break
        node = value
    return node


def _linear_call(call: ast.Call, tree: ast.AST) -> bool:
    if not isinstance(tree, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef):
        return False
    return any(
        isinstance(statement, ast.Assign | ast.AnnAssign | ast.Expr) and statement.value is call
        for statement in tree.body
    )


def _module_call(
    node: ast.Call, spec: ast.Call, scope: bindings.Scope, seen_calls: Set[ast.Call]
) -> ast.Call | None:
    if len(node.args) != 1 or node.keywords:
        return None
    module = _object_value(node.args[0], scope)
    if not isinstance(module, ast.Call) or module not in seen_calls:
        return None
    if len(module.args) != 1 or module.keywords:
        return None
    if scope.unmodified_origin(module.func) != "importlib.util.module_from_spec":
        return None
    return module if _object_value(module.args[0], scope) is spec else None


def _contains_object(node: ast.expr, objects: tuple[ast.Call, ...], scope: bindings.Scope) -> bool:
    pending = [node]
    seen: set[int] = set()
    while pending:
        value = _object_value(pending.pop(), scope)
        if value in objects:
            return True
        if id(value) not in seen:
            seen.add(id(value))
            pending.extend(
                child for child in ast.iter_child_nodes(value) if isinstance(child, ast.expr)
            )
    return False


def _exported_binding(
    node: ast.Assign | ast.AnnAssign | ast.NamedExpr | ast.AugAssign,
    exported: set[str],
    objects: tuple[ast.Call, ...],
    scope: bindings.Scope,
) -> bool:
    if node.value is None or not _contains_object(node.value, objects, scope):
        return False
    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    return isinstance(node, ast.NamedExpr | ast.AugAssign) or any(
        not isinstance(target, ast.Name) or target.id in exported for target in targets
    )


def _nonlocal_object_use(
    node: ast.AST, exported: set[str], objects: tuple[ast.Call, ...], scope: bindings.Scope
) -> bool:
    if isinstance(node, ast.Assign | ast.AnnAssign | ast.NamedExpr | ast.AugAssign):
        return _exported_binding(node, exported, objects, scope)
    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
        return any(
            isinstance(child, ast.expr) and _contains_object(child, objects, scope)
            for child in bindings.local_nodes(node)
        )
    if isinstance(node, ast.Return | ast.Yield | ast.YieldFrom):
        return node.value is not None and _contains_object(node.value, objects, scope)
    return False


def _local_bindings(
    tree: ast.AST, execution: ast.Call, objects: tuple[ast.Call, ...], scope: bindings.Scope
) -> bool:
    nodes = tuple(bindings.local_nodes(tree))
    exported = {
        name for node in nodes if isinstance(node, ast.Global | ast.Nonlocal) for name in node.names
    }
    return not any(
        getattr(node, "lineno", 0) <= execution.lineno
        and _nonlocal_object_use(node, exported, objects, scope)
        for node in nodes
    )


def _intact_objects(
    spec: ast.Call,
    module: ast.Call,
    execution: ast.Call,
    scope: bindings.Scope,
    seen_calls: Set[ast.Call],
    scope_tree: ast.AST,
) -> bool:
    if scope.unmodified_origin(spec.func) != "importlib.util.spec_from_file_location":
        return False
    objects = (spec, module)
    for written in scope.attribute_writes:
        if (written.lineno, written.col_offset) > (execution.lineno, execution.col_offset):
            continue
        if _contains_object(written.value, objects, scope):
            return False
    for call in scope.calls:
        if call is module or call is execution or call not in seen_calls:
            continue
        arguments = (call.func, *call.args, *(kw.value for kw in call.keywords))
        if any(_contains_object(arg, objects, scope) for arg in arguments):
            return False
    return _local_bindings(scope_tree, execution, objects, scope)


def prove_execution(
    node: ast.Call,
    scope: bindings.Scope,
    scope_tree: ast.AST,
    seen_calls: Set[ast.Call],
) -> ExecutionProof | None:
    """Recognize the exact loader operation and prove a prior local linear chain.

    ``seen_calls`` is the collector's already-visited Call set, preserving AST
    order. Unrelated operations return None; recognized opaque executions return
    a reason. This does not read source, execute code or interpret helpers.
    """
    method = node.func
    if not (
        isinstance(method, ast.Attribute)
        and method.attr == "exec_module"
        and isinstance(method.value, ast.Attribute)
        and method.value.attr == "loader"
    ):
        return None
    spec = _object_value(method.value.value, scope)
    if (
        not isinstance(spec, ast.Call)
        or scope.origin(spec.func) != "importlib.util.spec_from_file_location"
    ):
        return ExecutionProof(None, None, "File-loader execution has no proven spec factory")
    module = _module_call(node, spec, scope, seen_calls)
    if (
        spec not in seen_calls
        or module is None
        or not all(_linear_call(call, scope_tree) for call in (spec, module, node))
        or not _intact_objects(spec, module, node, scope, seen_calls, scope_tree)
    ):
        return ExecutionProof(
            spec, None, "File-loader spec, module or loader is opaque, changed or escaped"
        )
    if len(spec.args) != 2 or spec.keywords:
        return ExecutionProof(spec, None, "File-loader factory arguments are not supported")
    return ExecutionProof(spec, _object_value(spec.args[1], scope), None)


def input_domain(
    spec: ast.Call, scope: bindings.Scope, anchored_path: ast.expr | None
) -> tuple[tuple[str, str], ...] | None:
    """Bound runtime names and collector-anchored paths with the shared text limit.

    Independent finite domains conservatively produce a Cartesian product.
    Paths normalize like Path inputs, and absolute/traversing targets stay opaque.
    """
    if len(spec.args) != 2 or spec.keywords or anchored_path is None:
        return None
    names, paths = scope.strings(spec.args[0]), scope.strings(anchored_path)
    if not names or not paths or any("\0" in text for text in (*names, *paths)):
        return None
    combined = ast.JoinedStr(
        [
            ast.FormattedValue(spec.args[0], -1),
            ast.Constant("\0"),
            ast.FormattedValue(anchored_path, -1),
        ]
    )
    values = scope.strings(combined)
    if not values:
        return None
    return _relative_pairs(values)


def _relative_pairs(values: tuple[str, ...]) -> tuple[tuple[str, str], ...] | None:
    result = tuple(
        (name, target) for name, _, target in (value.partition("\0") for value in values)
    )
    if any(
        not name or Path(target).is_absolute() or ".." in Path(target).parts
        for name, target in result
    ):
        return None
    return tuple(dict.fromkeys((name, Path(target).as_posix()) for name, target in result))


def prior_calls(
    execution: ast.Call, scope: bindings.Scope
) -> Iterator[tuple[ast.Call, bindings.Scope]]:
    """Calls that cannot be proven later than this local execution.

    Local lexical calls retain their order. An ancestor's calls may precede a
    nested invocation regardless of its definition line; no caller is interpreted.
    The collector owns recognition and resource resolution in each supplied Scope.
    """
    current: bindings.Scope | None = scope
    while current is not None:
        for call in current.calls:
            if current is not scope or (call.lineno, call.col_offset) < (
                execution.lineno,
                execution.col_offset,
            ):
                yield call, current
        current = current.parent
