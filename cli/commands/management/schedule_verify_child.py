"""The per-script check child of `ava schedules verify` — stdin: one schedule script.

Run as `python -m cli.commands.management.schedule_verify_child` from the repo root, in the
checkout's own venv (the interpreter that would launch the schedule). Stdlib only at module level.
Two checks; nothing from the script's body runs:

1. **Imports** — `compile()` the script, then execute ONLY its top-level import statements. A moved
   module (the #2678 / R3 Wave-2 drift class) fails here.
2. **Call sites** — every call whose callee resolves through those imports to repo code
   (`catch_up(...)`, `schedules.catchup.fire_slot_once(...)`, `Database.from_settings()`) must
   `inspect.signature(...).bind` the arguments the script passes. A signature that moved on
   (2026-10-03: `catch_up()` and `fire_slot_once()` gained a required `db`) fails here, where the
   import check stays green. Only the argument SHAPE is bound (count and keyword names; values are
   placeholders), and a call that cannot be bound statically is skipped, never guessed: `*args` /
   `**kwargs` at the call site, a callee reached through an instance or a local variable, a name the
   script rebinds, a callable with no introspectable signature. The agent SDK (`ava.*`) is skipped
   too: plugins wrap its functions at load time (`ava.agents.spawn(label=...)`), so its static
   signature is not the call contract.

Output: the last stdout line is the verdict — `CHILD-OK`, `CHILD-COMPILE-ERROR:<line>:<msg>` (rc 3),
`CHILD-MODULE:<name>` (rc 4), `CHILD-EXC:<type>:<msg>` (rc 5), or `CHILD-SIG:<detail> | <detail>` (rc 6).
"""

from __future__ import annotations

import ast
import inspect
import sys
import types
from collections.abc import Iterator
from pathlib import Path
from typing import Any

_DETAIL_MAX = 160


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
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
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


def _resolve(namespace: dict[str, Any], chain: tuple[str, list[str]]) -> tuple[Any, str | None]:
    """The object a callee chain names, and (when a module lost the attribute) the missing name."""
    target: Any = namespace[chain[0]]
    for attr in chain[1]:
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
    problems = call_site_problems(tree, namespace, Path.cwd().resolve())
    if problems:
        print("CHILD-SIG:" + " | ".join(p.replace("\n", " ")[:_DETAIL_MAX] for p in problems))
        return 6
    print("CHILD-OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
