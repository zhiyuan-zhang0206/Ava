"""AST detection behind the ambient-state rule (see scripts/structure/ambient_state/__init__.py).

`scan(tree, rel, repo_root)` returns every `Hit` in one module: the state it
holds, the import-time effects it has, and the background work it starts. It
knows nothing about baselines or allowlists; the policy lives in
`allowlist.py`, the gate wiring in `__init__.py`.

Module-level statements (descending into `if`/`try`/`with`/loops, never into an
`if __name__ == "__main__":` block) are classified by shape:

- `ambient-container` — an empty mutable container (`{}`, `[]`, `set()`,
  `defaultdict(list)`, `deque(maxlen=n)`), or any container the module mutates
  somewhere (`_BUILT = [False]` with `_BUILT[0] = True`, a filled table that a
  function later `.update()`s). A never-mutated, non-empty literal is a constant.
- `ambient-instance` — `NAME = Foo(...)` or `NAME = make()` whose callee is not a
  write-only facade, framework wiring, a pure constant constructor, or a repo
  value class (frozen dataclass / NamedTuple / Enum / TypedDict / stateless
  class, resolved from its defining file). A holder object (`_State()`) is an
  instance like any other.
- `contextvar` — `NAME = ContextVar(...)`.
- `host-fact` / `import-time-read` — a value that reads the host (`sys.platform`,
  `os.name`, `platform.*`) or the ambient environment (`settings`, `os.environ`,
  the clock, the filesystem, random ids) while the module imports.
- `import-time-call` — a bare call statement at import (`register_x(...)`);
  declarative wiring (`app.include_router`, `parser.add_argument`) is exempt.
- `hidden-singleton` / `hidden-cache` — a module-level function wrapped in
  `lru_cache` / `cache`, with or without parameters.

Anywhere in the module: `global-rebind` (a function that declares `global X` and
rebinds it, or assigns through `globals()[...]`), `foreign-rebind` (assigning an
attribute of another first-party module), `class-level-container` (a mutable
container shared through a class attribute), and the free-floating background
work rules `asyncio-task` (`asyncio.create_task`, `asyncio.ensure_future`,
`<loop>.create_task`) and `thread` (`threading.Thread(...)`), keyed by the
enclosing function. A `<expr>.create_task(...)` whose receiver is not
`asyncio` or an event loop (a `TaskGroup`'s `tg.create_task`, a component's
`self._tg.create_task`) is the sanctioned shape; AST cannot prove the receiver is a
TaskGroup, so any receiver not named like a loop passes (a known gap).

Known gaps, by design of a per-file AST pass: a non-empty container mutated only
from another module, a `Thread` subclass instantiated elsewhere, and
`run_coroutine_threadsafe`/executor submits are not seen.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scripts.structure.ambient_state import allowlist as allow
from scripts.structure.ambient_state.module import (
    MUTATORS,
    Module,
    container_kind,
    dotted,
    is_declarative_class,
    is_main_guard,
    is_value_class,
    module_statements,
    target_names,
)

INSTANCE = "ambient-instance"
CONTAINER = "ambient-container"
CONTEXTVAR = "contextvar"
GLOBAL_REBIND = "global-rebind"
SINGLETON = "hidden-singleton"
CACHE = "hidden-cache"
READ = "import-time-read"
CALL = "import-time-call"
HOST = "host-fact"
FOREIGN = "foreign-rebind"
CLASS_CONTAINER = "class-level-container"
TASK = "asyncio-task"
THREAD = "thread"

_DECLARATIVE = re.compile(
    r"(\.|^)(include_router|add_api_route|add_exception_handler|add_middleware|middleware|mount"
    r"|add_argument|add_parser|set_defaults|add_subparsers|add_typer|command)$"
)
_SETTINGS_NAMES = frozenset({"settings", "turn_settings"})
_CACHE_DECORATORS = frozenset({"lru_cache", "cache", "alru_cache", "memoize", "cache_result"})
_LOOP_GETTERS = frozenset({"get_running_loop", "get_event_loop", "new_event_loop"})
_CLASSVAR = "ClassVar"
_CONTEXTVARS = frozenset({"contextvars.ContextVar"})
_TASK_SPAWNERS = frozenset({"asyncio.create_task", "asyncio.ensure_future"})
_THREAD_CLASSES = frozenset({"threading.Thread"})

# What a module-level value may read while the module imports. The host reads are
# their own rule: a platform constant is a fact about the machine the code runs on,
# to be injected as a `Platform` rather than recomputed per module.
_HOST_READS = frozenset({"sys.platform", "os.name"})
_HOST_READ_CALL_PREFIXES = ("platform.", "os.uname", "sys.getwindowsversion")
_READ_CALLS = frozenset(
    {
        "os.getenv", "os.getcwd", "os.path.expanduser", "os.getpid", "pathlib.Path.home",
        "pathlib.Path.cwd", "time.time", "time.monotonic", "time.perf_counter",
        "datetime.datetime.now", "datetime.datetime.utcnow", "uuid.uuid4", "uuid.uuid1",
        "socket.gethostname", "shutil.which", "tempfile.gettempdir", "open", "io.open",
        "json.load", "platform.node", "getpass.getuser",
    }
)  # fmt: skip
_READ_CALL_PREFIXES = ("random.", "secrets.")
_READ_METHODS = frozenset({"read_text", "read_bytes", "expanduser"})


@dataclass(frozen=True)
class Hit:
    """One finding: the rule that fired, the name it is keyed by, and its line."""

    rule: str
    name: str
    line: int


# ── shapes of values ───────────────────────────────────────────────────────


def _strip_cast(value: ast.expr) -> ast.expr:
    while (
        isinstance(value, ast.Call)
        and (dotted(value.func) or "") in {"cast", "typing.cast"}
        and len(value.args) == 2
    ):
        value = value.args[1]
    return value


def _is_pure_method_chain(call: ast.Call, module: Module) -> bool:
    """`"lit".join(...)`, `SQL.format(...)`, `Path(...).resolve()`: a method on a constant."""
    func = call.func
    if not isinstance(func, ast.Attribute):
        return False
    base = func.value
    if isinstance(base, ast.Constant):
        return True
    if isinstance(base, ast.Call):
        callee = module.full_name(base.func)
        return (
            callee in allow.PURE_CALLEES
            or callee in allow.PURE_CHAIN_CALLEES
            or _is_pure_method_chain(base, module)
        )
    return isinstance(base, ast.Name) and base.id in module.string_constants


def _is_exempt_callee(call: ast.Call, module: Module, callee: str) -> bool:
    return (
        callee in allow.PURE_CALLEES
        or callee in allow.WIRING_CALLEES
        or callee in allow.PURE_REPO_CALLEES
        or callee in allow.SINK_CALLEES
        or callee.endswith(allow.SINK_CALLEE_SUFFIXES)
        or _is_pure_method_chain(call, module)
        or is_value_class(callee, module)
    )


# ── what a value reads at import time ──────────────────────────────────────


def _settings_read(node: ast.Name, module: Module) -> str | None:
    origin = module.imports.get(node.id, ("", None))[0]
    return READ if origin.startswith("base.config") else None


def _attribute_read(node: ast.Attribute | ast.Name, module: Module) -> str | None:
    full = module.full_name(node)
    if full in _HOST_READS:
        return HOST
    return READ if full == "os.environ" or full.startswith("os.environ.") else None


def _call_read(node: ast.Call, module: Module) -> str | None:
    callee = module.full_name(node.func)
    if callee.startswith(_HOST_READ_CALL_PREFIXES):
        return HOST
    if callee in _READ_CALLS or callee.startswith(_READ_CALL_PREFIXES):
        return READ
    is_read_method = isinstance(node.func, ast.Attribute) and node.func.attr in _READ_METHODS
    return READ if is_read_method else None


def _read_of(node: ast.AST, module: Module) -> str | None:
    """The rule an expression node triggers by reading the environment, if any."""
    if isinstance(node, ast.Name) and node.id in _SETTINGS_NAMES:
        return _settings_read(node, module)
    if isinstance(node, ast.Attribute | ast.Name):
        return _attribute_read(node, module)
    return _call_read(node, module) if isinstance(node, ast.Call) else None


def import_time_read(value: ast.AST, module: Module) -> str | None:
    """`host-fact`, `import-time-read` or None; a host read wins over another read."""
    found: str | None = None
    stack = [value]
    while stack:
        node = stack.pop()
        if isinstance(node, ast.Lambda):
            continue
        rule = _read_of(node, module)
        if rule == HOST:
            return HOST
        found = found or rule
        stack.extend(ast.iter_child_nodes(node))
    return found


# ── module-level statements ────────────────────────────────────────────────


def _call_hit(value: ast.Call, module: Module) -> str | None:
    callee = module.full_name(value.func)
    if callee in _CONTEXTVARS:
        return CONTEXTVAR
    return None if _is_exempt_callee(value, module, callee) else INSTANCE


def _assigned_names(st: ast.Assign | ast.AnnAssign) -> list[str]:
    targets = st.targets if isinstance(st, ast.Assign) else [st.target]
    names = [name for target in targets for name in target_names(target)]
    return [name for name in names if not (name.startswith("__") and name.endswith("__"))]


def _value_rule(value: ast.expr, names: list[str], module: Module, mutated: set[str]) -> str | None:
    read = import_time_read(value, module)
    if read == HOST:
        return HOST
    is_container, empty = container_kind(value)
    rule: str | None = None
    if is_container:
        rule = CONTAINER if empty or any(name in mutated for name in names) else None
    elif isinstance(value, ast.Call):
        reads_itself = _call_read(value, module) or import_time_read(value.func, module)
        rule = READ if reads_itself else _call_hit(value, module)
    return rule or read


def _assignment_hits(
    st: ast.Assign | ast.AnnAssign, module: Module, mutated: set[str]
) -> list[Hit]:
    if st.value is None:
        return []
    names = _assigned_names(st)
    rule = _value_rule(_strip_cast(st.value), names, module, mutated) if names else None
    return [Hit(rule, name, st.lineno) for name in names] if rule else []


def _wiring_heads(module: Module) -> set[str]:
    heads: set[str] = set()
    for st in module_statements(module.tree.body):
        if (
            isinstance(st, ast.Assign | ast.AnnAssign)
            and isinstance(st.value, ast.Call)
            and module.full_name(st.value.func) in allow.WIRING_CALLEES
        ):
            targets = st.targets if isinstance(st, ast.Assign) else [st.target]
            heads.update(n for t in targets for n in target_names(t))
    return heads


def _call_statement_hit(st: ast.Expr, module: Module, heads: set[str]) -> list[Hit]:
    call = st.value
    if not isinstance(call, ast.Call) or isinstance(call.func, ast.Constant):
        return []
    raw = dotted(call.func) or ""
    head = raw.split(".")[0].removesuffix("()")
    if head in heads or (_DECLARATIVE.search(raw) and head in module.defs):
        return []
    return [Hit(CALL, module.full_name(call.func), st.lineno)]


def _decorator_hit(st: ast.FunctionDef | ast.AsyncFunctionDef) -> list[Hit]:
    for decorator in st.decorator_list:
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        if (dotted(target) or "").rsplit(".", 1)[-1] in _CACHE_DECORATORS:
            takes_args = bool(st.args.args or st.args.kwonlyargs)
            return [Hit(CACHE if takes_args else SINGLETON, st.name, st.lineno)]
    return []


def _module_level_hits(module: Module, mutated: set[str]) -> list[Hit]:
    hits: list[Hit] = []
    heads = _wiring_heads(module)
    for st in module_statements(module.tree.body):
        if isinstance(st, ast.Assign | ast.AnnAssign):
            hits.extend(_assignment_hits(st, module, mutated))
        elif isinstance(st, ast.Expr):
            hits.extend(_call_statement_hit(st, module, heads))
        elif isinstance(st, ast.FunctionDef | ast.AsyncFunctionDef):
            hits.extend(_decorator_hit(st))
    return hits


# ── mutation ───────────────────────────────────────────────────────────────


def _strip_subscripts(expr: ast.expr) -> ast.expr:
    while isinstance(expr, ast.Subscript):
        expr = expr.value
    return expr


def _mutated_root(expr: ast.expr) -> ast.expr:
    """Strip subscripts and attribute hops down to the object that is being changed."""
    while isinstance(expr, ast.Subscript | ast.Attribute):
        expr = expr.value
    return expr


def mutation_targets(node: ast.AST) -> list[ast.expr]:
    """Objects one statement or call changes in place: `X[k] = v`, `X[k] += v`,
    `del X[k]`, `X += v` and `X.append(v)`."""
    if isinstance(node, ast.Assign | ast.Delete):
        return [t.value for t in node.targets if isinstance(t, ast.Subscript)]
    if isinstance(node, ast.AugAssign):
        return [node.target.value if isinstance(node.target, ast.Subscript) else node.target]
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        return [node.func.value] if node.func.attr in MUTATORS else []
    return []


# ── classes ────────────────────────────────────────────────────────────────


def _class_attribute(st: ast.stmt) -> tuple[str, ast.expr, ast.expr | None] | None:
    """(name, value, annotation) of a class-body `name = value` / `name: T = value`."""
    if isinstance(st, ast.Assign) and len(st.targets) == 1 and isinstance(st.targets[0], ast.Name):
        return st.targets[0].id, st.value, None
    if isinstance(st, ast.AnnAssign) and isinstance(st.target, ast.Name) and st.value is not None:
        return st.target.id, st.value, st.annotation
    return None


def _class_mutated_attributes(cls: ast.ClassDef) -> set[str]:
    """Class attributes some method mutates through `cls.X`, `self.X` or `<Class>.X`."""
    owners = {"cls", "self", cls.name}
    names: set[str] = set()
    for node in ast.walk(cls):
        for target in mutation_targets(node):
            held = _strip_subscripts(target)
            if (
                isinstance(held, ast.Attribute)
                and isinstance(held.value, ast.Name)
                and held.value.id in owners
            ):
                names.add(held.attr)
    return names


def _class_containers(cls: ast.ClassDef) -> list[tuple[str, int, bool]]:
    """(name, line, empty) of each mutable container in the class body that is shared
    state. Enum members and NamedTuple / TypedDict / dataclass / pydantic fields declare
    fields, so they only count when annotated `ClassVar`."""
    declarative = is_declarative_class(cls)
    found: list[tuple[str, int, bool]] = []
    for st in cls.body:
        attribute = _class_attribute(st)
        if attribute is None:
            continue
        name, value, annotation = attribute
        is_container, empty = container_kind(value)
        class_var = annotation is not None and _CLASSVAR in ast.unparse(annotation)
        if is_container and (class_var or not declarative) and not name.startswith("__"):
            found.append((name, st.lineno, empty))
    return found


def class_hits(cls: ast.ClassDef, qualname: str) -> list[Hit]:
    """`class-level-container`: a mutable container shared through a class attribute,
    empty or mutated by a method."""
    found = _class_containers(cls)
    mutated: set[str] = (
        _class_mutated_attributes(cls) if any(not empty for *_, empty in found) else set()
    )
    return [
        Hit(CLASS_CONTAINER, f"{qualname}.{name}", line)
        for name, line, empty in found
        if empty or name in mutated
    ]


# ── the scoped walk: global rebinds, foreign rebinds, mutations, background work ──


@dataclass
class _Frame:
    name: str
    is_function: bool
    line: int
    locals: set[str]  # parameters and names the body assigns
    imported: set[str]  # names a function-level import binds (they stay module aliases)
    globals: set[str]


class _ScopeExit:
    """Marks the end of a function or class body on the walk stack."""


_EXIT = _ScopeExit()


def _parameters(node: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    args = node.args
    names = {a.arg for a in [*args.posonlyargs, *args.args, *args.kwonlyargs]}
    names.update(a.arg for a in (args.vararg, args.kwarg) if a is not None)
    return names


class _Walker:
    def __init__(self, module: Module) -> None:
        self.module = module
        self.frames: list[_Frame] = []
        self.hits: list[Hit] = []
        self.mutated: set[str] = set()
        self.loop_names: set[str] = set()
        self._handlers: dict[type[ast.AST], Callable[[Any], None]] = {
            ast.Name: self._on_name,
            ast.Call: self._on_call,
            ast.Assign: self._on_store,
            ast.AugAssign: self._on_store,
            ast.AnnAssign: self._on_store,
            ast.Delete: self._on_store,
            ast.Import: self._on_import,
            ast.ImportFrom: self._on_import,
            ast.Global: self._on_global,
            ast.ExceptHandler: self._on_except,
        }

    def run(self) -> None:
        stack: list[ast.AST | _ScopeExit] = [self.module.tree]
        while stack:
            node = stack.pop()
            if isinstance(node, _ScopeExit):
                self._leave()
                continue
            if isinstance(node, ast.If) and is_main_guard(node.test):
                continue
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                self._enter(node)
                stack.append(_EXIT)
            handler = self._handlers.get(type(node))
            if handler is not None:
                handler(node)
            stack.extend(reversed(list(ast.iter_child_nodes(node))))

    # scopes
    def _enter(self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> None:
        self._bind(node.name)
        is_function = not isinstance(node, ast.ClassDef)
        params: set[str] = set() if isinstance(node, ast.ClassDef) else _parameters(node)
        self.frames.append(_Frame(node.name, is_function, node.lineno, params, set(), set()))
        if isinstance(node, ast.ClassDef):
            self.hits.extend(class_hits(node, self._qualname()))

    def _leave(self) -> None:
        frame = self.frames.pop()
        if frame.is_function:
            self.hits.extend(
                Hit(GLOBAL_REBIND, name, frame.line)
                for name in sorted(frame.globals & (frame.locals | frame.imported))
            )

    def _qualname(self) -> str:
        return ".".join(f.name for f in self.frames) or "<module>"

    def _function_frame(self) -> _Frame | None:
        return next((f for f in reversed(self.frames) if f.is_function), None)

    def _bind(self, name: str, *, imported: bool = False) -> None:
        frame = self._function_frame()
        if frame is not None:
            (frame.imported if imported else frame.locals).add(name)

    def _shadowed(self, name: str) -> bool:
        return any(
            f.is_function and name in f.locals and name not in f.globals for f in self.frames
        )

    # nodes
    def _on_name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Store | ast.Del):
            self._bind(node.id)

    def _on_import(self, node: ast.Import | ast.ImportFrom) -> None:
        self.module.bind_import(node)
        for alias in node.names:
            self._bind((alias.asname or alias.name).split(".")[0], imported=True)

    def _on_global(self, node: ast.Global) -> None:
        if (frame := self._function_frame()) is not None:
            frame.globals.update(node.names)

    def _on_except(self, node: ast.ExceptHandler) -> None:
        if node.name:
            self._bind(node.name)

    def _note_mutations(self, node: ast.AST) -> None:
        for target in mutation_targets(node):
            root = _mutated_root(target)
            if isinstance(root, ast.Name) and not self._shadowed(root.id):
                self.mutated.add(root.id)

    def _on_store(self, node: ast.Assign | ast.AugAssign | ast.AnnAssign | ast.Delete) -> None:
        if isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name):
            self._bind(node.target.id)  # `x += 1` makes x local unless it is declared global
        self._note_mutations(node)
        targets = node.targets if isinstance(node, ast.Assign | ast.Delete) else [node.target]
        for target in targets:
            self._store_target(target, node.lineno)
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Call)
            and _is_loop_getter(node.value)
        ):
            self.loop_names.update(n for t in node.targets for n in target_names(t))

    def _store_target(self, target: ast.expr, line: int) -> None:
        if isinstance(target, ast.Attribute):
            self._foreign(target, line)
        elif isinstance(target, ast.Subscript) and _is_globals_call(target.value):
            self.hits.append(Hit(GLOBAL_REBIND, "globals()", line))

    def _foreign(self, target: ast.Attribute, line: int) -> None:
        head = (dotted(target.value) or "").split(".")[0]
        if not head or self._shadowed(head):
            return
        base = self.module.module_of(target.value)
        if base is not None and base != self.module.qualifier:
            self.hits.append(Hit(FOREIGN, f"{base}.{target.attr}", line))

    def _on_call(self, node: ast.Call) -> None:
        self._note_mutations(node)
        rule = self._spawn_rule(node)
        if rule:
            self.hits.append(Hit(rule, self._qualname(), node.lineno))
        elif self._is_setattr_on_module(node):
            self.hits.append(Hit(FOREIGN, "setattr", node.lineno))

    def _is_setattr_on_module(self, node: ast.Call) -> bool:
        if not (isinstance(node.func, ast.Name) and node.func.id == "setattr" and node.args):
            return False
        base = self.module.module_of(node.args[0])
        return base is not None and base != self.module.qualifier

    def _spawn_rule(self, node: ast.Call) -> str | None:
        callee = self.module.full_name(node.func)
        if callee in _TASK_SPAWNERS:
            return TASK
        if callee in _THREAD_CLASSES:
            return THREAD
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "create_task":
            return TASK if self._is_loop(func.value) else None
        return None

    def _is_loop(self, receiver: ast.expr) -> bool:
        if isinstance(receiver, ast.Call):
            return _is_loop_getter(receiver)
        if isinstance(receiver, ast.Name):
            return receiver.id in self.loop_names or receiver.id.lower().endswith("loop")
        return isinstance(receiver, ast.Attribute) and receiver.attr.lower().endswith("loop")


def _is_loop_getter(call: ast.Call) -> bool:
    return (dotted(call.func) or "").rsplit(".", 1)[-1] in _LOOP_GETTERS


def _is_globals_call(node: ast.expr) -> bool:
    return (
        isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "globals"
    )


# ── entry point ────────────────────────────────────────────────────────────


def scan(tree: ast.Module, rel: str, repo_root: Path) -> list[Hit]:
    """Every ambient-state hit in one module, before any allowlist is applied."""
    module = Module(tree, rel, repo_root)
    walker = _Walker(module)
    walker.run()
    return [*_module_level_hits(module, walker.mutated), *walker.hits]
