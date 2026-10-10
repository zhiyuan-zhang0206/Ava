"""Local Task-valued maps, exact snapshots and one awaited owner join.

This supplements Task identity and error receipts without proving admission,
resolving arbitrary delegates or changing a runtime cancellation policy.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator

from scripts.structure.ambient_state.module import Module, dotted
from scripts.structure.ambient_state.owner_method import (
    Function,
    assignment_pairs,
    call_nodes,
    executable_nodes,
    instance_field,
    join_has_timeout,
)

_DROPS = {
    "clear",
    "pop",
    "popitem",
    "discard",
    "remove",
    "__delitem__",
    "update",
    "setdefault",
    "__setitem__",
    "__ior__",
}


def inline_registration(statements: list[ast.stmt], task: str) -> tuple[str, str] | None:
    """An original Task is stored as a value, then immediately observed."""
    statement = statements[0]
    if not isinstance(statement, ast.Assign) or len(statement.targets) != 1:
        return None
    target = statement.targets[0]
    if (
        not isinstance(target, ast.Subscript)
        or not isinstance(statement.value, ast.Name)
        or statement.value.id != task
        or (registry := instance_field(target.value)) is None
    ):
        return None
    if len(statements) < 2:
        return None
    following = statements[1]
    if not isinstance(following, ast.Expr) or not isinstance(following.value, ast.Call):
        return None
    call = following.value
    if dotted(call.func) != f"{task}.add_done_callback" or len(call.args) != 1:
        return None
    callback = instance_field(call.args[0])
    return (registry, callback.removeprefix("self.")) if callback is not None else None


def _source(
    value: ast.expr, registry: str, aliases: dict[str, bool], module: Module
) -> bool | None:
    """False is the original Task-valued mapping; True is its Task values."""
    if instance_field(value) == registry:
        return False
    if isinstance(value, ast.Name):
        return aliases.get(value.id)
    if not isinstance(value, ast.Call) or value.keywords:
        return None
    if isinstance(value.func, ast.Attribute) and value.func.attr == "values" and not value.args:
        return True if _source(value.func.value, registry, aliases, module) is False else None
    if module.full_name(value.func) in {"dict", "set"} and len(value.args) == 1:
        source = _source(value.args[0], registry, aliases, module)
        return source if source == (module.full_name(value.func) == "set") else None
    return None


def _snapshot_sources(
    method: Function, registry: str, module: Module, bound: dict[str, bool]
) -> dict[str, bool]:
    aliases = dict(bound)
    assignments = list(assignment_pairs(method))
    for target, value in reversed(assignments):
        if isinstance(target, ast.Name):
            source = _source(value, registry, aliases, module)
            if source is not None:
                aliases[target.id] = source
    return aliases


def _aliases(
    method: Function, registry: str, module: Module, bound: dict[str, bool]
) -> dict[str, bool]:
    aliases = _snapshot_sources(method, registry, module, bound)
    assignments = list(assignment_pairs(method))
    for name in list(aliases):
        if any(
            isinstance(target, ast.Name)
            and target.id == name
            and _source(value, registry, aliases, module) != aliases[name]
            for target, value in assignments
        ) or not _unchanged(method, {name}):
            aliases.pop(name)
    return aliases


def _receiver(node: ast.expr) -> str | None:
    return node.id if isinstance(node, ast.Name) else instance_field(node)


def _slot(target: ast.expr, receivers: set[str]) -> bool:
    return isinstance(target, ast.Subscript) and _receiver(target.value) in receivers


def _slots_modified(method: Function, receivers: set[str]) -> bool:
    return any(_slot(target, receivers) for target, _ in assignment_pairs(method)) or any(
        isinstance(node, ast.AugAssign) and _receiver(node.target) in receivers
        for node in executable_nodes(method)
    )


def _slots_deleted(method: Function, receivers: set[str]) -> bool:
    return any(
        isinstance(node, ast.Delete) and any(_slot(target, receivers) for target in node.targets)
        for node in executable_nodes(method)
    )


def _unchanged(method: Function, receivers: set[str]) -> bool:
    """Neither a snapshot nor retained map may lose or replace original slots."""
    mutates = any(
        isinstance(call.func, ast.Attribute)
        and _receiver(call.func.value) in receivers
        and call.func.attr in _DROPS
        for call in call_nodes(method)
    )
    return not (mutates or _slots_modified(method, receivers) or _slots_deleted(method, receivers))


def _joined(method: Function, registry: str, aliases: dict[str, bool], module: Module) -> bool:
    if any(isinstance(statement, ast.Return | ast.Raise) for statement in method.body[:-1]):
        return False
    return any(
        isinstance(node, ast.Await)
        and isinstance(node.value, ast.Call)
        and module.full_name(node.value.func) == "asyncio.wait"
        and bool(node.value.args)
        and _source(node.value.args[0], registry, aliases, module) is True
        and any(kw.arg == "timeout" for kw in node.value.keywords)
        and join_has_timeout(node.value)
        for node in executable_nodes(method)
    )


def _cancelled(method: Function, registry: str, aliases: dict[str, bool], module: Module) -> bool:
    for node in executable_nodes(method):
        if not isinstance(node, ast.For) or not isinstance(node.target, ast.Name):
            continue
        if _source(node.iter, registry, aliases, module) is not True:
            continue
        task = node.target.id
        if any(
            isinstance(target, ast.Name) and target.id == task
            for target, _ in assignment_pairs(node)
        ):
            continue
        if any(dotted(call.func) == f"{task}.cancel" for call in call_nodes(node)):
            return True
    return False


def _returns_get(value: ast.expr | None, registry: str, params: set[str]) -> bool:
    if not isinstance(value, ast.Call) or not isinstance(value.func, ast.Attribute):
        return False
    return (
        instance_field(value.func.value) == registry
        and value.func.attr == "get"
        and len(value.args) == 1
        and not value.keywords
        and isinstance(value.args[0], ast.Name)
        and value.args[0].id in params
    )


def _lookup(method: Function, registry: str) -> bool:
    if not isinstance(method, ast.FunctionDef):
        return False
    body = method.body
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        body = body[1:]
    if len(body) != 1 or not isinstance(body[0], ast.Return):
        return False
    arguments = method.args.posonlyargs + method.args.args
    return _returns_get(body[0].value, registry, {param.arg for param in arguments[1:]})


def _map_created(methods: dict[str, Function], registry: str, module: Module) -> bool:
    init = methods.get("__init__")
    if init is None:
        return False
    values = [
        value for target, value in assignment_pairs(init) if instance_field(target) == registry
    ]
    return len(values) == 1 and (
        isinstance(values[0], ast.Dict)
        or (isinstance(values[0], ast.Call) and module.full_name(values[0].func) == "dict")
    )


def _awaited_helpers(
    method: Function, methods: dict[str, Function]
) -> Iterator[tuple[ast.Call, ast.AsyncFunctionDef]]:
    for node in executable_nodes(method):
        if not isinstance(node, ast.Await) or not isinstance(node.value, ast.Call):
            continue
        call = node.value
        name = instance_field(call.func)
        helper = methods.get(name.removeprefix("self.")) if name else None
        if isinstance(helper, ast.AsyncFunctionDef):
            yield call, helper


def _helper_join(
    call: ast.Call,
    helper: ast.AsyncFunctionDef,
    registry: str,
    aliases: dict[str, bool],
    module: Module,
) -> bool:
    params = helper.args.posonlyargs + helper.args.args
    bound = {
        param.arg: source
        for param, arg in zip(params[1:], call.args, strict=False)
        if (source := _source(arg, registry, aliases, module)) is not None
    }
    snapshots = _snapshot_sources(helper, registry, module, bound)
    if not _unchanged(helper, {registry, *snapshots}):
        return False
    return _joined(helper, registry, _aliases(helper, registry, module, bound), module)


def map_span(
    method: Function, methods: dict[str, Function], registry: str, module: Module
) -> list[Function]:
    """Stop cancels original values, joins them and retains observable slots."""
    if not isinstance(method, ast.AsyncFunctionDef) or not _map_created(methods, registry, module):
        return []
    if not any(_lookup(candidate, registry) for candidate in methods.values()):
        return []
    aliases = _aliases(method, registry, module, {})
    snapshots = _snapshot_sources(method, registry, module, {})
    if not _unchanged(method, {registry, *snapshots}) or not _cancelled(
        method, registry, aliases, module
    ):
        return []
    if _joined(method, registry, aliases, module):
        return [method]
    for call, helper in _awaited_helpers(method, methods):
        if _helper_join(call, helper, registry, aliases, module):
            return [method, helper]
    return []
