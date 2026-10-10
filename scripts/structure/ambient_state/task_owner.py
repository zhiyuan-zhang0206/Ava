"""Necessary same-instance Task ownership wiring, not a lifecycle proof.

Only immediate synchronous registration and one layer of actual same-class
helper edges are supported. Admission, routing/retirement across branches,
finite runtime budgets, primary errors and root teardown require runtime tests.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from itertools import pairwise

from scripts.structure.ambient_state.module import Module, dotted
from scripts.structure.ambient_state.task_owner_span import inline_registration, map_span
from scripts.structure.ambient_state.thread_owner import (
    Function,
    _assignments,
    _bounded_join,
    _calls,
    _field,
    nodes,
)


def _statements(root: ast.AST) -> Iterator[list[ast.stmt]]:
    """Statement lists without entering nested definitions or constant-dead code."""
    for _, value in ast.iter_fields(root):
        if isinstance(value, list) and value and isinstance(value[0], ast.stmt):
            yield value
    for node in ast.iter_child_nodes(root):
        if isinstance(node, Function | ast.ClassDef | ast.Lambda):
            continue
        if isinstance(node, ast.If) and isinstance(node.test, ast.Constant):
            yield node.body if node.test.value else node.orelse
        else:
            yield from _statements(node)


def _name(node: ast.AST) -> str | None:
    return node.id if isinstance(node, ast.Name) else _field(node)


def _early_exit(method: Function) -> bool:
    return any(isinstance(statement, ast.Return | ast.Raise) for statement in method.body[:-1])


def _one_helper(method: Function, methods: dict[str, Function]) -> list[Function]:
    names = {_field(call.func) for call in _calls(method)}
    # Properties referenced as results are an actual edge too.
    properties = {
        name
        for name, helper in methods.items()
        if any(dotted(d) == "property" for d in helper.decorator_list)
    }
    names.update(
        _field(node)
        for node in nodes(method)
        if isinstance(node, ast.Attribute) and node.attr in properties
    )
    return [
        method,
        *(
            helper
            for name, helper in methods.items()
            if isinstance(helper, ast.FunctionDef)
            and not _early_exit(helper)
            and f"self.{name}" in names
        ),
    ]


def _init_fields(methods: dict[str, Function], module: Module) -> set[str]:
    init = methods.get("__init__")
    if init is None:
        return set()
    assignments = list(_assignments(init))
    fields = [_field(target) for target, _ in assignments]
    return {
        field
        for target, value in assignments
        if (field := _field(target)) is not None
        and fields.count(field) == 1
        and (
            isinstance(value, ast.Dict | ast.Set)
            or (isinstance(value, ast.Call) and module.full_name(value.func) in {"dict", "set"})
        )
    }


def _replaced(
    field: str, methods: dict[str, Function], allowed: tuple[Function, str] | None = None
) -> bool:
    return any(
        _field(target) == field
        and not (allowed and method is allowed[0] and _name(value) == allowed[1])
        for method in methods.values()
        if method.name != "__init__"
        for target, value in _assignments(method)
    ) or any(
        isinstance(call.func, ast.Attribute)
        and _field(call.func.value) == field
        and call.func.attr == "clear"
        for method in methods.values()
        for call in _calls(method)
    )


def _registration(
    statement: ast.stmt, task: str, methods: dict[str, Function]
) -> tuple[str, str] | None:
    if not isinstance(statement, ast.Expr) or not isinstance(statement.value, ast.Call):
        return None
    invocation = statement.value
    helper_name = _field(invocation.func)
    helper = methods.get(helper_name.removeprefix("self.")) if helper_name else None
    if not isinstance(helper, ast.FunctionDef):
        return None
    if any(
        isinstance(
            node, ast.Await | ast.Yield | ast.YieldFrom | ast.Return | ast.Delete | ast.Raise
        )
        for node in nodes(helper)
    ):
        return None
    params = helper.args.posonlyargs + helper.args.args
    bound = next(
        (
            param.arg
            for param, arg in zip(params[1:], invocation.args, strict=False)
            if _name(arg) == task
        ),
        None,
    )
    if bound is None or _rebound(helper, bound):
        return None
    registries = _registered_in(helper, bound)
    callbacks = _callbacks(helper, bound)
    if len(registries) != 1 or len(callbacks) != 1 or _drops_registration(helper, registries):
        return None
    return next(iter(registries)), callbacks[0].removeprefix("self.")


def _drops_registration(helper: Function, registries: set[str]) -> bool:
    return any(
        isinstance(call.func, ast.Attribute)
        and _field(call.func.value) in registries
        and call.func.attr in {"pop", "popitem", "remove", "discard"}
        for call in _calls(helper)
    )


def _registered_in(helper: Function, bound: str) -> set[str]:
    registries = {
        field
        for target, _ in _assignments(helper)
        if isinstance(target, ast.Subscript)
        and _name(target.slice) == bound
        and (field := _field(target.value)) is not None
    }
    registries.update(
        field
        for call in _calls(helper)
        if isinstance(call.func, ast.Attribute)
        and call.func.attr == "add"
        and (field := _field(call.func.value)) is not None
        and len(call.args) == 1
        and _name(call.args[0]) == bound
    )
    return registries


def _callbacks(helper: Function, bound: str) -> list[str]:
    return [
        callback
        for call in _calls(helper)
        if isinstance(call.func, ast.Attribute)
        and _name(call.func.value) == bound
        and call.func.attr == "add_done_callback"
        and len(call.args) == 1
        and (callback := _field(call.args[0])) is not None
    ]


def _reads_error(callback: Function) -> str | None:
    params = callback.args.posonlyargs + callback.args.args
    if len(params) != 2 or _early_exit(callback):
        return None
    task = params[1].arg
    cancelled = any(dotted(call.func) == f"{task}.cancelled" for call in _calls(callback))
    if _rebound(callback, task) or not cancelled:
        return None
    for target, value in _assignments(callback):
        if not isinstance(target, ast.Name) or not isinstance(value, ast.Call):
            continue
        if (
            dotted(value.func) == f"{task}.exception"
            and not value.args
            and not _rebound(callback, target.id, value)
        ):
            return target.id
    return None


def _rebound(method: Function, name: str, original: ast.expr | None = None) -> bool:
    return any(
        _name(target) == name and value is not original for target, value in _assignments(method)
    )


def _error_paths(
    callback: Function, error: str, methods: dict[str, Function]
) -> Iterator[tuple[Function, str]]:
    yield callback, error
    for call in _calls(callback):
        name = _field(call.func)
        helper = methods.get(name.removeprefix("self.")) if name else None
        if not isinstance(helper, ast.FunctionDef):
            continue
        params = helper.args.posonlyargs + helper.args.args
        for param, arg in zip(params[1:], call.args, strict=False):
            if _name(arg) == error and not _rebound(helper, param.arg):
                yield helper, param.arg


def _receipt(method: Function, error: str, module: Module) -> str | None:
    if _early_exit(method):
        return None
    stored = _stored_errors(method, error)
    for call in _calls(method):
        if isinstance(call.func, ast.Attribute) and _field(call.func.value) in stored:
            continue
        if module.full_name(call.func) in {"str", "repr", "type", "isinstance", "bool"}:
            continue
        if any(_name(node) == error for node in nodes(call)):
            return next(iter(stored)) if len(stored) == 1 else None
    return None


def _stored_errors(method: Function, error: str) -> set[str]:
    stored = {
        field
        for target, value in _assignments(method)
        if _name(value) == error and (field := _field(target)) is not None
    }
    stored.update(
        field
        for call in _calls(method)
        if isinstance(call.func, ast.Attribute)
        and call.func.attr == "append"
        and (field := _field(call.func.value)) is not None
        and len(call.args) == 1
        and _name(call.args[0]) == error
    )
    return stored


def _real_receipt(
    receipt: str, method: Function, error: str, methods: dict[str, Function], module: Module
) -> bool:
    if any(
        _field(target) == receipt and _name(value) == error
        for target, value in _assignments(method)
    ):
        return True
    init = methods.get("__init__")
    if init is None:
        return False
    values = [value for target, value in _assignments(init) if _field(target) == receipt]
    if len(values) != 1:
        return False
    value = values[0]
    return isinstance(value, ast.List) or (
        isinstance(value, ast.Call) and module.full_name(value.func) == "list"
    )


def _raises(method: Function, receipt: str) -> bool:
    aliases = _receipt_aliases(method, receipt)
    return any(
        isinstance(node, ast.Raise)
        and node.exc is not None
        and (_receipt_value(node.exc, receipt) or _name(node.exc) in aliases)
        for node in nodes(method)
    )


def _receipt_value(value: ast.AST, receipt: str) -> bool:
    return _field(value) == receipt or (
        isinstance(value, ast.Subscript)
        and _field(value.value) == receipt
        and isinstance(value.slice, ast.Constant)
        and value.slice.value == 0
    )


def _receipt_aliases(method: Function, receipt: str) -> set[str]:
    aliases: set[str] = set()
    for target, value in _assignments(method):
        original = _receipt_value(value, receipt)
        if original and isinstance(target, ast.Name) and not _rebound(method, target.id, value):
            aliases.add(target.id)
        if (
            original
            and isinstance(target, ast.Tuple)
            and target.elts
            and isinstance(target.elts[0], ast.Name)
            and not _rebound(method, target.elts[0].id)
        ):
            aliases.add(target.elts[0].id)
    return aliases


def _registry_tasks(method: Function, registry: str) -> set[str]:
    return {
        node.target.id
        for node in nodes(method)
        if isinstance(node, ast.For | ast.comprehension)
        and isinstance(node.target, ast.Name)
        and _field(node.iter) == registry
    }


def _pending_names(method: Function, registry: str, module: Module) -> set[str]:
    return {
        target.id
        for target, value in _assignments(method)
        if isinstance(target, ast.Name)
        and _registry_snapshot(value, registry, module)
        and not _rebound(method, target.id, value)
    }


def _registry_snapshot(value: ast.expr, registry: str, module: Module) -> bool:
    if isinstance(value, ast.Call) and module.full_name(value.func) == "set":
        return len(value.args) == 1 and _field(value.args[0]) == registry
    return isinstance(value, ast.SetComp) and any(
        _field(gen.iter) == registry and _name(gen.target) == _name(value.elt)
        for gen in value.generators
    )


def _waited(method: Function, registry: str, module: Module) -> bool:
    if _early_exit(method):
        return False
    pending = _pending_names(method, registry, module)
    for node in nodes(method):
        if not isinstance(node, ast.Await) or not isinstance(node.value, ast.Call):
            continue
        call = node.value
        if module.full_name(call.func) != "asyncio.wait" or not call.args:
            continue
        same = (
            _field(call.args[0]) == registry
            or _name(call.args[0]) in pending
            or _registry_snapshot(call.args[0], registry, module)
        )
        if same and any(kw.arg == "timeout" for kw in call.keywords) and _bounded_join(call):
            return True
    return False


def _unfinished(method: Function, registry: str) -> bool:
    return any(
        isinstance(node, ast.Return)
        and node.value is not None
        and any(_live_identities(part, registry) for part in [node.value, *nodes(node.value)])
        for node in nodes(method)
    )


def _live_identities(node: ast.AST, registry: str) -> bool:
    if (
        not isinstance(node, ast.GeneratorExp | ast.ListComp | ast.SetComp)
        or len(node.generators) != 1
    ):
        return False
    gen = node.generators[0]
    task = _name(gen.target)
    if task is None or _field(gen.iter) != registry:
        return False
    identified = _name(node.elt) == task or (
        isinstance(node.elt, ast.Call) and dotted(node.elt.func) == f"{task}.get_name"
    )
    checked = any(_not_done(condition, task) for condition in gen.ifs)
    return identified and checked


def _not_done(condition: ast.expr, task: str) -> bool:
    return (
        isinstance(condition, ast.UnaryOp)
        and isinstance(condition.op, ast.Not)
        and isinstance(condition.operand, ast.Call)
        and dotted(condition.operand.func) == f"{task}.done"
    )


def _request_stop(
    helpers: list[Function], methods: dict[str, Function], registry: str, module: Module
) -> bool:
    """A real owner-created signal is set and read by its registered worker."""
    signals = _signal_fields(methods, module)
    requested = _requested_signals(helpers, signals)
    workers = _registered_workers(methods, registry, module)
    native = _native_stop(helpers, methods, workers, module)
    return native or any(
        _awaits_signal(methods[name], requested, module) for name in workers if name in methods
    )


def _signal_fields(methods: dict[str, Function], module: Module) -> set[str]:
    init = methods.get("__init__")
    if init is None:
        return set()
    return {
        field
        for target, value in _assignments(init)
        if (field := _field(target)) is not None
        and _owner_signal(value, module)
        and not _replaced(field, methods)
    }


def _owner_signal(value: ast.expr, module: Module) -> bool:
    if not isinstance(value, ast.Call):
        return False
    if module.full_name(value.func) == "asyncio.Event":
        return True
    return (
        isinstance(value.func, ast.Attribute)
        and value.func.attr == "create_future"
        and isinstance(value.func.value, ast.Call)
        and module.full_name(value.func.value.func) == "asyncio.get_running_loop"
    )


def _requested_signals(helpers: list[Function], signals: set[str]) -> set[str]:
    return {
        field
        for helper in helpers
        for call in _calls(helper)
        if isinstance(call.func, ast.Attribute)
        and call.func.attr in {"set", "set_result"}
        and (field := _field(call.func.value)) in signals
    }


def _registered_workers(methods: dict[str, Function], registry: str, module: Module) -> set[str]:
    workers: set[str] = set()
    for method in methods.values():
        for body in _statements(method):
            for statement, following in pairwise(body):
                pair = _spawn_assignment(statement)
                if pair is not None:
                    name = _registered_worker(pair, following, methods, registry, module)
                    if name is not None:
                        workers.add(name)
    return workers


def _registered_worker(
    pair: tuple[str, ast.Call],
    following: ast.stmt,
    methods: dict[str, Function],
    registry: str,
    module: Module,
) -> str | None:
    task, call = pair
    if module.full_name(call.func) not in {"asyncio.create_task", "asyncio.ensure_future"}:
        return None
    registration = _registration(following, task, methods)
    if registration is None or registration[0] != registry or not call.args:
        return None
    work = call.args[0]
    if isinstance(work, ast.Call) and (name := _field(work.func)) is not None:
        return name.removeprefix("self.")
    return None


def _awaits_signal(worker: Function, requested: set[str], module: Module) -> bool:
    return any(
        field in requested
        for node in nodes(worker)
        if isinstance(node, ast.Await)
        for field in _waited_signals(node.value, module)
    )


def _native_stop(
    helpers: list[Function], methods: dict[str, Function], workers: set[str], module: Module
) -> bool:
    """EOF on an actually retained Popen pipe, consumed by the same worker wait.

    The EOF protocol and the native deadline remain runtime obligations. A close
    call on an unrelated or fabricated handle is insufficient.
    """
    native = _retained_processes(methods, module)
    closed = _closed_processes(helpers, native)
    return any(
        _awaits_process(methods[name], closed, module) for name in workers if name in methods
    )


def _retained_processes(methods: dict[str, Function], module: Module) -> set[str]:
    native: set[str] = set()
    invalid: set[str] = set()
    for method in methods.values():
        sources = _popen_sources(method, module)
        for target, value in _assignments(method):
            field = _field(target)
            if field is None:
                continue
            if _name(value) in sources:
                native.add(field)
            elif not (
                method.name == "__init__"
                and isinstance(value, ast.Constant)
                and value.value is None
            ):
                invalid.add(field)
    return native - invalid


def _popen_sources(method: Function, module: Module) -> set[str]:
    return {
        target.id
        for target, value in _assignments(method)
        if isinstance(target, ast.Name)
        and isinstance(value, ast.Call)
        and module.full_name(value.func) == "subprocess.Popen"
        and not _rebound(method, target.id, value)
    }


def _process_aliases(method: Function) -> dict[str, str]:
    aliases = {
        target.id: field
        for target, value in _assignments(method)
        if isinstance(target, ast.Name)
        and (field := _field(value)) is not None
        and not _rebound(method, target.id, value)
    }
    for target, value in _assignments(method):
        if isinstance(target, ast.Tuple) and isinstance(value, ast.Tuple):
            for part, raw in zip(target.elts, value.elts, strict=False):
                field = _field(raw)
                if (
                    isinstance(part, ast.Name)
                    and field is not None
                    and not _rebound(method, part.id)
                ):
                    aliases[part.id] = field
    return aliases


def _process_receiver(node: ast.expr, aliases: dict[str, str]) -> str | None:
    return aliases.get(node.id) if isinstance(node, ast.Name) else _field(node)


def _closed_processes(helpers: list[Function], native: set[str]) -> set[str]:
    closed: set[str] = set()
    for helper in helpers:
        aliases = _process_aliases(helper)
        for call in _calls(helper):
            field = _closed_pipe(call, aliases)
            if field in native:
                closed.add(field)
    return closed


def _closed_pipe(call: ast.Call, aliases: dict[str, str]) -> str | None:
    if not isinstance(call.func, ast.Attribute) or call.func.attr != "close":
        return None
    pipe = call.func.value
    if not isinstance(pipe, ast.Attribute) or pipe.attr != "stdin":
        return None
    return _process_receiver(pipe.value, aliases)


def _awaits_process(worker: Function, closed: set[str], module: Module) -> bool:
    aliases = _process_aliases(worker)
    return any(
        _waited_process(node.value, aliases, module) in closed
        for node in nodes(worker)
        if isinstance(node, ast.Await) and isinstance(node.value, ast.Call)
    )


def _waited_process(call: ast.Call, aliases: dict[str, str], module: Module) -> str | None:
    if module.full_name(call.func) != "asyncio.to_thread" or len(call.args) < 2:
        return None
    wait = call.args[0]
    if not isinstance(wait, ast.Attribute) or wait.attr != "wait":
        return None
    bounded = ast.Call(func=wait, args=[call.args[1]], keywords=[])
    return _process_receiver(wait.value, aliases) if _bounded_join(bounded) else None


def _waited_signals(value: ast.expr, module: Module) -> set[str]:
    if not isinstance(value, ast.Call):
        return set()
    if (
        isinstance(value.func, ast.Attribute)
        and value.func.attr == "wait"
        and (field := _field(value.func.value)) is not None
    ):
        return {field}
    if module.full_name(value.func) == "asyncio.wait" and value.args:
        return {field for node in nodes(value.args[0]) if (field := _field(node)) is not None}
    return set()


def _map_teardown(
    methods: dict[str, Function], registry: str, receipt: str, module: Module
) -> bool:
    return any(
        any(_raises(helper, receipt) for helper in span)
        for method in methods.values()
        if (span := map_span(method, methods, registry, module))
    )


def _teardown(methods: dict[str, Function], registry: str, receipt: str, module: Module) -> bool:
    for method in methods.values():
        if not isinstance(method, ast.AsyncFunctionDef) or not _waited(method, registry, module):
            continue
        helpers = _one_helper(method, methods)
        cancelled = any(
            isinstance(call.func, ast.Attribute)
            and call.func.attr == "cancel"
            and _name(call.func.value) in _registry_tasks(helper, registry)
            for helper in helpers
            for call in _calls(helper)
        )
        raised = any(_raises(helper, receipt) for helper in helpers)
        unfinished = any(_unfinished(helper, registry) for helper in helpers)
        requested = _request_stop(helpers, methods, registry, module)
        if (cancelled or requested) and raised and unfinished:
            return True
    return False


def owned_calls(module: Module) -> set[int]:
    """Call ids with necessary real registry, result, error and teardown wiring."""
    accepted: set[int] = set()
    for cls in (node for node in ast.walk(module.tree) if isinstance(node, ast.ClassDef)):
        methods = {node.name: node for node in cls.body if isinstance(node, Function)}
        fields = _init_fields(methods, module)
        for method in methods.values():
            for body in _statements(method):
                for index, (statement, following) in enumerate(pairwise(body)):
                    pair = _spawn_assignment(statement)
                    if pair is None:
                        continue
                    task, call = pair
                    registration = _registration(following, task, methods)
                    values = registration is None
                    if values:
                        registration = inline_registration(body[index + 1 :], task)
                    if registration and _owned(
                        registration, methods, fields, module, values=values
                    ):
                        accepted.add(id(call))
    return accepted


def _spawn_assignment(statement: ast.stmt) -> tuple[str, ast.Call] | None:
    if not isinstance(statement, ast.Assign | ast.AnnAssign):
        return None
    targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
    value = statement.value
    if len(targets) != 1 or not isinstance(value, ast.Call) or (task := _name(targets[0])) is None:
        return None
    return task, value


def _owned(
    registration: tuple[str, str],
    methods: dict[str, Function],
    fields: set[str],
    module: Module,
    *,
    values: bool = False,
) -> bool:
    registry, callback_name = registration
    callback = methods.get(callback_name)
    if (
        registry not in fields
        or _replaced(registry, methods)
        or not isinstance(callback, ast.FunctionDef)
    ):
        return False
    error = _reads_error(callback)
    if error is None:
        return False
    for method, name in _error_paths(callback, error, methods):
        receipt = _receipt(method, name, module)
        if (
            receipt
            and _real_receipt(receipt, method, name, methods, module)
            and not _replaced(receipt, methods, (method, name))
            and (_map_teardown if values else _teardown)(methods, registry, receipt, module)
        ):
            return True
    return False
