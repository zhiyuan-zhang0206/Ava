"""The per-script check child of `ava schedules verify` — stdin: one schedule script.

Run as `python -m cli.commands.management.schedule_verify_child` from the repo root, in the
checkout's own venv (the interpreter that would launch the schedule). Stdlib only at module level.
Three checks; nothing from the script's body runs:

1. **Imports** — `compile()` the script, then execute ONLY its top-level import statements. A moved
   module (the #2678 / R3 Wave-2 drift class) fails here.
2. **Names** — every `Name` the script reads (any scope) must be one it binds, a builtin, or an
   implicit module global (`__name__`, ...). This is the read the import check cannot see: after the
   `shared` -> `base` rename, `_ROOT = Path(base.__file__)` reads a `base` nothing imports — the
   import check stays green and the first real fire dies with `NameError` (2026-10-01 audit: three
   of 16 copies). Conservative by construction: a name bound anywhere counts as bound (a PEP 695
   type parameter's own name included: `def f[T]`, `class C[T]`, `type A[T]`), annotation
   expressions are never read (house style: `from __future__ import annotations`; type-parameter
   bounds and defaults likewise), and a `from x import *` disables the check outright. Names only
   a runtime would create (`exec`/`eval`, `globals()` writes) are not modeled: a read of one is
   reported, never guessed around.
3. **Call sites** — every call whose callee resolves through those imports to repo code
   (`catch_up(...)`, `schedules.catchup.fire_slot_once(...)`, `Database.from_settings()`) must
   `inspect.signature(...).bind` the arguments the script passes. A signature that moved on
   (2026-10-03: `catch_up()` and `fire_slot_once()` gained a required `db`) fails here, where the
   import check stays green. Only the argument SHAPE is bound (count and keyword names; values are
   placeholders), and a call that cannot be bound statically is skipped, never guessed: `*args` /
   `**kwargs` at the call site, a callee reached through an instance or a local variable, a name the
   script rebinds, a callable with no introspectable signature. The agent SDK (`ava.*`) is skipped
   too, names and signatures alike: plugins install its namespaces (`ava.tasks`) and wrap its
   functions at load time (`ava.agents.spawn(label=...)`), so the static module is neither the
   set of names nor the call contract.

Output: the last stdout line is the verdict — `CHILD-OK`, `CHILD-COMPILE-ERROR:<line>:<msg>` (rc 3),
`CHILD-MODULE:<name>` (rc 4), `CHILD-EXC:<type>:<msg>` (rc 5), `CHILD-UNDEF:<detail> | <detail>` (rc 6),
or `CHILD-SIG:<detail> | <detail>` (rc 7).
"""

from __future__ import annotations

import ast
import builtins
import inspect
import sys
import types
from collections.abc import Iterator
from pathlib import Path
from typing import Any

_DETAIL_MAX = 160

# What a script may read without binding it: the builtins and the module-internal implicit
# globals the runtime provides.
_BUILTIN_NAMES = frozenset(dir(builtins))
_IMPLICIT_GLOBALS = frozenset(
    {
        "__annotations__",
        "__builtins__",
        "__class__",
        "__debug__",
        "__dict__",
        "__doc__",
        "__file__",
        "__loader__",
        "__name__",
        "__package__",
        "__spec__",
    }
)


def _rebound_names(tree: ast.Module) -> set[str]:
    """Names the script binds anywhere other than its top-level imports; a call through one of
    these is not (provably) a call of the imported object."""
    top_imports = {id(node) for node in tree.body if isinstance(node, ast.Import | ast.ImportFrom)}
    names: set[str] = set()
    for node in ast.walk(tree):
        if id(node) in top_imports:
            continue
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store | ast.Del):
            names.add(node.id)
        elif isinstance(
            node,
            ast.FunctionDef
            | ast.AsyncFunctionDef
            | ast.ClassDef
            | ast.TypeVar
            | ast.ParamSpec
            | ast.TypeVarTuple,
        ):
            names.add(node.name)
        elif isinstance(node, ast.arg):
            names.add(node.arg)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
        elif isinstance(node, ast.Import | ast.ImportFrom):
            names.update((a.asname or a.name).split(".")[0] for a in node.names)
    return names


def _callee_chain(func: ast.expr) -> tuple[str, list[str]] | None:
    """`a.b.c` -> ("a", ["b", "c"]); anything not rooted in a plain name -> None."""
    attrs: list[str] = []
    while isinstance(func, ast.Attribute):
        attrs.append(func.attr)
        func = func.value
    if not isinstance(func, ast.Name):
        return None
    return func.id, attrs[::-1]


def _is_repo_code(obj: object, root: Path) -> bool:
    """Defined in the checkout (not the venv), and not in the plugin-wrapped `ava` SDK package."""
    try:
        source = inspect.getsourcefile(inspect.unwrap(obj))  # pyright: ignore[reportArgumentType]
    except (TypeError, ValueError):
        return False
    if source is None:
        return False
    path = Path(source).resolve()
    if not path.is_relative_to(root) or {".venv", "site-packages"} & set(path.parts):
        return False
    return path.relative_to(root).parts[0] != "ava"


def _is_sdk(target: object) -> bool:
    """The `ava` SDK package or a submodule: its surface is installed by plugins at load time."""
    return isinstance(target, types.ModuleType) and target.__name__.split(".")[0] == "ava"


def _resolve(namespace: dict[str, Any], chain: tuple[str, list[str]]) -> tuple[Any, str | None]:
    """The object a callee chain names, and (when a module lost the attribute) the missing name."""
    target: Any = namespace[chain[0]]
    for attr in chain[1]:
        if _is_sdk(target):
            return None, None
        if isinstance(target, types.ModuleType) and not hasattr(target, attr):
            return None, f"{target.__name__} has no `{attr}`"
        try:
            target = getattr(target, attr)
        except AttributeError:
            return None, None
    return target, None


def _static_calls(
    tree: ast.Module, namespace: dict[str, Any]
) -> Iterator[tuple[ast.Call, tuple[str, list[str]]]]:
    """Calls rooted in a name the imports bound and the script never rebinds, with a fixed
    argument shape (no `*args` / `**kwargs` at the call site)."""
    rebound = _rebound_names(tree)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        chain = _callee_chain(node.func)
        if chain is None or chain[0] in rebound or chain[0] not in namespace:
            continue
        if any(isinstance(arg, ast.Starred) for arg in node.args):
            continue
        if any(kw.arg is None for kw in node.keywords):
            continue
        yield node, chain


def _problem(
    node: ast.Call, chain: tuple[str, list[str]], namespace: dict[str, Any], root: Path
) -> str | None:
    dotted = ".".join([chain[0], *chain[1]])
    target, missing = _resolve(namespace, chain)
    if missing is not None:
        return f"L{node.lineno} {dotted}(): {missing}"
    if target is None or not callable(target) or not _is_repo_code(target, root):
        return None
    try:
        signature = inspect.signature(target)
    except (TypeError, ValueError):
        return None
    positional = [object() for _ in node.args]
    keywords = {kw.arg: object() for kw in node.keywords if kw.arg}
    try:
        signature.bind(*positional, **keywords)
    except TypeError as exc:
        return f"L{node.lineno} {dotted}(): {exc} (is {dotted}{signature})"
    return None


def call_site_problems(tree: ast.Module, namespace: dict[str, Any], root: Path) -> list[str]:
    """One `L<line> <callee>(): <why>` per call whose arguments the callee cannot bind."""
    found = (
        _problem(node, chain, namespace, root) for node, chain in _static_calls(tree, namespace)
    )
    return [problem for problem in found if problem is not None]


def _bound_names(tree: ast.Module) -> set[str]:
    """Every name the script binds: its rebinds (`_rebound_names`, which excludes the top-level
    imports), those imports themselves, the pattern-matching captures, and what the language
    provides."""
    bound = _rebound_names(tree)
    for node in tree.body:
        if isinstance(node, ast.Import | ast.ImportFrom):
            bound.update((alias.asname or alias.name).split(".")[0] for alias in node.names)
    for node in ast.walk(tree):
        if isinstance(node, ast.MatchAs | ast.MatchStar) and node.name:
            bound.add(node.name)
        elif isinstance(node, ast.MatchMapping) and node.rest:
            bound.add(node.rest)
    return bound | _BUILTIN_NAMES | _IMPLICIT_GLOBALS


class _NameReads(ast.NodeVisitor):
    """First line of every `Name` read, annotations excluded: the house style imports
    `from __future__ import annotations`, so annotation expressions are never evaluated."""

    def __init__(self) -> None:
        self.first_line: dict[str, int] = {}

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Load):
            self.first_line.setdefault(node.id, node.lineno)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self.visit(node.target)
        if node.value is not None:
            self.visit(node.value)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        """Decorators, bases, keywords and body read as usual; type-parameter bounds/defaults are
        annotation-like and never read."""
        for child in (*node.decorator_list, *node.bases, *node.keywords, *node.body):
            self.visit(child)

    def visit_TypeAlias(self, node: ast.TypeAlias) -> None:
        """Only the value reads; the name binds and type-parameter bounds/defaults never read."""
        self.visit(node.value)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        self._visit_defaults(node.args)
        self.visit(node.body)

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        for decorator in node.decorator_list:
            self.visit(decorator)
        self._visit_defaults(node.args)
        for statement in node.body:
            self.visit(statement)

    def _visit_defaults(self, arguments: ast.arguments) -> None:
        """Only the default expressions: an arg node contributes nothing but an annotation."""
        for default in (*arguments.defaults, *arguments.kw_defaults):
            if default is not None:
                self.visit(default)


def undefined_name_problems(tree: ast.Module) -> list[str]:
    """One `L<line> <name>` per name the script reads but never binds.

    The conservative F821-shaped read: a name bound anywhere counts as bound, a
    `from x import *` leaves the file's name set open and disables the check. What
    remains is the class the import check cannot see — a statement reading a name
    nothing at all binds (#2678-adjacent; 2026-10-01 audit).
    """
    if any(
        isinstance(node, ast.ImportFrom) and any(alias.name == "*" for alias in node.names)
        for node in ast.walk(tree)
    ):
        return []
    reads = _NameReads()
    reads.visit(tree)
    bound = _bound_names(tree)
    unbound = {name: line for name, line in reads.first_line.items() if name not in bound}
    return [f"L{line} {name}" for name, line in sorted(unbound.items(), key=lambda item: item[1])]


def main() -> int:
    src = sys.stdin.read()
    try:
        compile(src, "<schedule>", "exec")
        tree = ast.parse(src)
    except SyntaxError as exc:
        print(f"CHILD-COMPILE-ERROR:{exc.lineno}:{exc.msg}")
        return 3
    segments = [
        segment
        for node in tree.body
        if isinstance(node, ast.Import | ast.ImportFrom)
        and (segment := ast.get_source_segment(src, node))
    ]
    namespace: dict[str, Any] = {"__name__": "dryimport"}
    try:
        exec(compile("\n".join(segments), "<imports-only>", "exec"), namespace)
    except ModuleNotFoundError as exc:
        print(f"CHILD-MODULE:{exc.name}")
        return 4
    except Exception as exc:
        print(f"CHILD-EXC:{type(exc).__name__}:{str(exc)[:120].replace(chr(10), ' ')}")
        return 5
    namespace.pop("__builtins__", None)
    undefined = undefined_name_problems(tree)
    if undefined:
        print("CHILD-UNDEF:" + " | ".join(name[:_DETAIL_MAX] for name in undefined))
        return 6
    problems = call_site_problems(tree, namespace, Path.cwd().resolve())
    if problems:
        print("CHILD-SIG:" + " | ".join(p.replace("\n", " ")[:_DETAIL_MAX] for p in problems))
        return 7
    print("CHILD-OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
