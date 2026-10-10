"""Conservative structural evidence for an explicitly owned stdlib Thread.

This is not a lifecycle proof: root wiring, ordering across branches, blocking
calls, dynamic deadlines and failure impact require consumer tests and review.
Unrecognized ownership stays a finding rather than an exemption or marker.
"""

from __future__ import annotations

import ast
import math
from collections.abc import Iterator

from scripts.structure.ambient_state.module import Module, dotted

Function = ast.FunctionDef | ast.AsyncFunctionDef


def nodes(root: ast.AST) -> Iterator[ast.AST]:
    """Walk executable statements without treating nested definitions as executed."""
    pending: list[ast.AST] = (
        [root] if isinstance(root, ast.Call) else list(ast.iter_child_nodes(root))
    )
    while pending:
        node = pending.pop()
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | ast.Lambda):
            continue
        yield node
        if isinstance(node, ast.If) and isinstance(node.test, ast.Constant):
            branch = node.body if node.test.value else node.orelse
            pending.extend([node.test, *branch])
        else:
            pending.extend(ast.iter_child_nodes(node))


def _field(node: ast.AST) -> str | None:
    name = dotted(node)
    return name if name and name.startswith("self.") and name.count(".") == 1 else None


def _assignments(root: ast.AST) -> Iterator[tuple[ast.expr, ast.expr]]:
    for node in nodes(root):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                yield target, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            yield node.target, node.value


def _aliases(root: ast.AST) -> dict[str, str]:
    return {
        target.id: field
        for target, value in _assignments(root)
        if isinstance(target, ast.Name) and (field := _field(value)) is not None
    }


def _receiver(node: ast.expr, aliases: dict[str, str]) -> str | None:
    return aliases.get(node.id) if isinstance(node, ast.Name) else _field(node)


def _calls(root: ast.AST) -> Iterator[ast.Call]:
    return (node for node in nodes(root) if isinstance(node, ast.Call))


def _method_calls(root: ast.AST) -> Iterator[tuple[str, str, ast.Call]]:
    aliases = _aliases(root)
    for call in _calls(root):
        if isinstance(call.func, ast.Attribute):
            receiver = _receiver(call.func.value, aliases)
            if receiver:
                yield receiver, call.func.attr, call


def _closure(method: Function, methods: dict[str, Function]) -> list[Function]:
    found: dict[str, Function] = {}
    pending = [method]
    while pending:
        current = pending.pop()
        if current.name in found:
            continue
        found[current.name] = current
        for call in _calls(current):
            name = _field(call.func)
            if name and (helper := methods.get(name.removeprefix("self."))) is not None:
                pending.append(helper)
    return list(found.values())


def _bounded_join(call: ast.Call) -> bool:
    timeout = next((kw.value for kw in call.keywords if kw.arg == "timeout"), None)
    timeout = timeout if timeout is not None else (call.args[0] if call.args else None)
    if timeout is None:
        return False
    if isinstance(timeout, ast.Constant):
        return (
            isinstance(timeout.value, int | float)
            and not isinstance(timeout.value, bool)
            and math.isfinite(timeout.value)
        )
    return not (isinstance(timeout, ast.Call) and dotted(timeout.func) == "float")


def _handle(call: ast.Call, method: Function) -> str | None:
    assignments = list(_assignments(method))
    for target, value in assignments:
        if value is call:
            field = _field(target)
            if field:
                return field
            if isinstance(target, ast.Name):
                return next(
                    (
                        _field(held)
                        for held, alias in assignments
                        if isinstance(alias, ast.Name) and alias.id == target.id and _field(held)
                    ),
                    None,
                )
    return None


def _reported_exception(handler: ast.ExceptHandler, module: Module, stored_at: int) -> bool:
    for call in _calls(handler):
        if call.lineno <= stored_at:
            continue
        if module.full_name(call.func) in {"str", "repr", "type", "isinstance", "bool"}:
            continue
        arguments = [*call.args, *(kw.value for kw in call.keywords)]
        if any(isinstance(arg, ast.Name) and arg.id == handler.name for arg in arguments):
            return True
    return False


def _stored_exception(handler: ast.ExceptHandler) -> tuple[str, int] | None:
    for statement in handler.body:
        if not isinstance(statement, ast.Assign | ast.AnnAssign):
            continue
        value = statement.value
        targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
        if isinstance(value, ast.Name) and value.id == handler.name:
            for target in targets:
                if field := _field(target):
                    return field, statement.lineno
    return None


def _overwrites_receipt(handler: ast.ExceptHandler, receipt: str) -> bool:
    return any(
        _field(target) == receipt and not (isinstance(value, ast.Name) and value.id == handler.name)
        for target, value in _assignments(handler)
    )


def _error_receipt(handler: ast.ExceptHandler, module: Module) -> str | None:
    if handler.type is None or module.full_name(handler.type) not in {
        "BaseException",
        "builtins.BaseException",
    }:
        return None
    stored = _stored_exception(handler)
    if not handler.name or stored is None:
        return None
    receipt, line = stored
    if _overwrites_receipt(handler, receipt) or not _reported_exception(handler, module, line):
        return None
    return receipt


def _target_receipt(
    target: Function, module: Module, events: set[str]
) -> tuple[str, ast.Try] | None:
    body = target.body
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        body = body[1:]
    # Requiring the complete entry under one try prevents a token catch around
    # an unrelated statement from blessing unobserved work before/after it.
    if len(body) != 1 or not isinstance(body[0], ast.Try):
        return None
    attempt = body[0]
    if attempt.orelse or not _completion_only(attempt.finalbody, events):
        return None
    if len(attempt.handlers) != 1:
        return None
    if any(isinstance(node, ast.Return) for handler in attempt.handlers for node in nodes(handler)):
        return None
    for handler in attempt.handlers:
        receipt = _error_receipt(handler, module)
        if receipt:
            return receipt, attempt
    return None


def _raises(root: Function, receipt: str) -> bool:
    aliases = _aliases(root)
    return any(
        isinstance(node, ast.Raise)
        and node.exc is not None
        and _receiver(node.exc, aliases) == receipt
        for node in nodes(root)
    )


def _event_fields(methods: dict[str, Function], module: Module) -> set[str]:
    return {
        field
        for method in methods.values()
        if method.name == "__init__"
        for target, value in _assignments(method)
        if (field := _field(target))
        and isinstance(value, ast.Call)
        and module.full_name(value.func) == "threading.Event"
    }


def _completion_only(body: list[ast.stmt], events: set[str]) -> bool:
    for statement in body:
        if not isinstance(statement, ast.Expr) or not isinstance(statement.value, ast.Call):
            return False
        call = statement.value
        if not isinstance(call.func, ast.Attribute) or call.func.attr != "set":
            return False
        if _field(call.func.value) not in events or call.args or call.keywords:
            return False
    return True


def _join_and_observe(calls: list[tuple[str, str, ast.Call]], handle: str) -> bool:
    joined = any(
        receiver == handle and name == "join" and _bounded_join(call)
        for receiver, name, call in calls
    )
    observed = any(receiver == handle and name == "is_alive" for receiver, name, _ in calls)
    return joined and observed


def _teardown(
    methods: dict[str, Function],
    handle: str,
    receipt: str,
    signals: set[str],
) -> bool:
    for method in methods.values():
        closure = _closure(method, methods)
        calls = [entry for helper in closure for entry in _method_calls(helper)]
        stopped = any(receiver in signals and name == "set" for receiver, name, _ in calls)
        raised = any(_raises(helper, receipt) for helper in closure)
        if stopped and raised and _join_and_observe(calls, handle):
            return True
    return False


def owned_calls(module: Module) -> set[int]:
    """Ids of Thread constructors with visible owner/stop/completion/error wiring."""
    accepted: set[int] = set()
    for cls in (node for node in ast.walk(module.tree) if isinstance(node, ast.ClassDef)):
        methods = {node.name: node for node in cls.body if isinstance(node, Function)}
        events = _event_fields(methods, module)
        handles = _constructor_handles(methods, module)
        for method in methods.values():
            if method.name != "__init__":
                continue
            for call in _calls(method):
                if module.full_name(call.func) != "threading.Thread":
                    continue
                handle = _handle(call, method)
                if handles.count(handle) == 1 and _owned(call, method, methods, events, module):
                    accepted.add(id(call))
    return accepted


def _owned(
    call: ast.Call,
    method: Function,
    methods: dict[str, Function],
    events: set[str],
    module: Module,
) -> bool:
    handle = _handle(call, method)
    target = next((kw.value for kw in call.keywords if kw.arg == "target"), None)
    target_name = _field(target) if target is not None else None
    worker = methods.get(target_name.removeprefix("self.")) if target_name else None
    if not handle or worker is None:
        return False
    outcome = _target_receipt(worker, module, events)
    if outcome is None:
        return False
    receipt, _ = outcome
    signals = _worker_signals(_closure(worker, methods), events)
    started = any(
        receiver == handle and name == "start" for receiver, name, _ in _method_calls(method)
    )
    return started and _teardown(methods, handle, receipt, signals)


def _signals_passed(helper: Function, events: set[str]) -> set[str]:
    signals: set[str] = set()
    for invocation in _calls(helper):
        arguments = [*invocation.args, *(kw.value for kw in invocation.keywords)]
        for argument in arguments:
            field = _field(argument)
            if field is not None and field in events:
                signals.add(field)
    return signals


def _worker_signals(closure: list[Function], events: set[str]) -> set[str]:
    signals: set[str] = set()
    for helper in closure:
        signals.update(
            receiver
            for receiver, name, _ in _method_calls(helper)
            if receiver in events and name in {"wait", "is_set"}
        )
        signals.update(_signals_passed(helper, events))
    return signals


def _constructor_handles(methods: dict[str, Function], module: Module) -> list[str | None]:
    return [
        _handle(call, method)
        for method in methods.values()
        for call in _calls(method)
        if module.full_name(call.func) == "threading.Thread"
    ]
