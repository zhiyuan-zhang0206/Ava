"""Public API contract snapshots for the core package doors.

A door's contract is its **public** surface (Rule 4 in `scripts/lint_code_structure.py`:
nothing outside a package may import its `_`-private modules or names). This tool
renders that public surface as a human-readable snapshot file next to the door's
code, so a PR that changes a contract shows an explicit snapshot diff — the same
discipline `db/schema.sql` and `ui/web/openapi.json` already carry. A body-only
change (an implementation fix with no signature change) must leave the snapshot
byte-identical; that property is the whole point.

Run: `.venv/bin/python scripts/structure/contracts.py --write` to regenerate every
snapshot, `--check` to verify them (also wired as the `contracts-snapshot-fresh`
pre-commit hook).

**Pure AST — this module never imports a door's own code.** The door modules load
Settings and other process state; importing them here would make a lint tool boot
the application. Every renderer works off `ast.parse` output only.

## What belongs to a door

Every module under the door's package, recursively, whose path has no component
starting with `_` (checked against the leading-underscore rule `_is_private` below —
`__init__.py` is not private, since it starts with `__`). A private module or
private subpackage is excluded along with everything it contains. A door may also
name a single top-level module file directly (`shared/db.py`) instead of a package.

## Public surface of one module

- If the module assigns a literal `__all__` (a list or tuple of `str` constants),
  exactly those names are public — regardless of a leading underscore, and
  including a name that is merely `from x import y` (no `as`) when `y` is listed.
- Otherwise, public means top-level (module-body-level, not nested in a function
  or `if`) definitions whose name does not start with `_`: `def`/`async def`,
  `class`, a simple-`Name` assignment target (`Assign`/`AnnAssign`), a PEP 695
  `type X = ...` alias, or an explicit re-export (`from x import y as y`,
  `import x as x` — the redundant-alias idiom this repo already uses throughout
  its package doors to satisfy ruff's unused-import check while declaring intent).
  A plain `from x import y` without `as y` binds a *local* name for the module's
  own use; it is not a re-export unless `__all__` says otherwise.

## Rendering

Every expression (signatures, bases, annotations, values) goes through
`ast.unparse`. A value only prints in full when `ast.literal_eval` accepts it and
the unparsed text is at most 100 characters (a "short literal") — event-kind
strings and enum values are exactly this and belong in the contract; a computed
or oversized value renders as `...` instead. **Type-expression values are the
exception and always render in full, however long**: an old-style alias
(`Category = Literal["audit", "telemetry", "log"]`, `X = A | B`) is as much a
contract as a PEP 695 `type X = ...` one, and truncating its members to `...`
would hide exactly the diff a reviewer needs to see. A value counts as a type
expression when it is a `Subscript` whose base is a typing/builtin generic
(`Literal`, `Union`, `Optional`, `Annotated`, `Callable`, `Mapping`, `Sequence`,
`Iterable`, `tuple`, `list`, `dict`, `set`, `frozenset`, `type`, `TypeAlias`),
a `|`-`BinOp` of such expressions or plain names, or any value at all on an
assignment annotated `: TypeAlias`. Default **presence** is also contract for an
`AnnAssign` field (dataclass / NamedTuple / TypedDict / pydantic — whether a
constructor argument is required), module-level or class-level alike: a short
literal default prints in full, a non-literal default prints as `...` (still
present, so the field reads as optional), and no default at all renders the
bare `name: annotation`. A class's members (public methods — plus the handful
of structural dunders `__init__`/`__call__`/`__enter__`/`__exit__`/
`__aenter__`/`__aexit__`/`__iter__` — fields, enum-style Assigns, and nested
classes) are sorted by name and indented two spaces per nesting level; a
module's entries (functions, classes, variables, re-exports) are likewise
sorted by name. Modules within a door are sorted by dotted name. Output always
ends with exactly one trailing newline and uses `\n` line endings.
"""

from __future__ import annotations

import ast
import difflib
import sys
from collections.abc import Callable
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_COMMAND = ".venv/bin/python scripts/structure/contracts.py --write"

# (dotted door, source path, snapshot path) — all repo-relative POSIX paths.
DOORS: tuple[tuple[str, str, str], ...] = (
    ("shared.db", "shared/db.py", "shared/db.api.txt"),
    ("shared.agents", "shared/agents", "shared/agents/api.txt"),
    ("shared.events", "shared/events", "shared/events/api.txt"),
)

_ALLOWED_DUNDERS = frozenset(
    {
        "__init__",
        "__call__",
        "__enter__",
        "__exit__",
        "__aenter__",
        "__aexit__",
        "__iter__",
    }
)
_LITERAL_ERRORS = (ValueError, TypeError, SyntaxError, MemoryError, RecursionError)


def _is_private(part: str) -> bool:
    return part.startswith("_") and not part.startswith("__")


# --- door membership -----------------------------------------------------------


def _door_is_public(rel_path: str) -> bool:
    """No path component (directory or filename stem) starts with a single `_`."""
    parts = rel_path.removesuffix(".py").split("/")
    return not any(_is_private(part) for part in parts)


def _door_modules(source: str, repo_root: Path) -> list[str]:
    """Repo-relative `.py` paths belonging to one door: the file itself for a
    single-module door, else every public module under the package, recursively."""
    path = repo_root / source
    if path.is_file():
        return [source]
    return [
        rel
        for file in path.rglob("*.py")
        if _door_is_public(rel := file.relative_to(repo_root).as_posix())
    ]


def _dotted_name(rel_path: str) -> str:
    """A module file's dotted name — a package's own `__init__.py` is the package."""
    dotted = rel_path.removesuffix(".py").replace("/", ".")
    return dotted.removesuffix(".__init__")


# --- literal-value helpers ------------------------------------------------------


def _short_literal_text(node: ast.expr) -> str | None:
    """The unparsed text of `node` when it is a literal at most 100 chars, else None."""
    try:
        ast.literal_eval(node)
    except _LITERAL_ERRORS:
        return None
    text = ast.unparse(node)
    return text if len(text) <= 100 else None


def _value_or_ellipsis(node: ast.expr) -> str:
    text = _short_literal_text(node)
    return text if text is not None else "..."


# --- type-expression values (always rendered in full) ---------------------------

# Typing/builtin generics whose subscript is itself contract, not a runtime value
# (an old-style alias like `Category = Literal["audit", "telemetry", "log"]`).
_TYPE_GENERIC_BASES = frozenset(
    {
        "Literal",
        "Union",
        "Optional",
        "Annotated",
        "Callable",
        "Mapping",
        "Sequence",
        "Iterable",
        "tuple",
        "list",
        "dict",
        "set",
        "frozenset",
        "type",
        "TypeAlias",
    }
)


def _subscript_base_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _is_type_expr(node: ast.expr) -> bool:
    if isinstance(node, ast.Subscript):
        return _subscript_base_name(node.value) in _TYPE_GENERIC_BASES
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return all(
            isinstance(side, ast.Name) or _is_type_expr(side) for side in (node.left, node.right)
        )
    return False


def _is_type_alias_annotation(annotation: ast.expr | None) -> bool:
    if isinstance(annotation, ast.Name):
        return annotation.id == "TypeAlias"
    return isinstance(annotation, ast.Attribute) and annotation.attr == "TypeAlias"


def _value_suffix(value: ast.expr, annotation: ast.expr | None) -> str:
    """The rendered value: full `ast.unparse` for a type expression (however
    long), else the short-literal-or-`...` fallback."""
    if _is_type_alias_annotation(annotation) or _is_type_expr(value):
        return ast.unparse(value)
    return _value_or_ellipsis(value)


# --- __all__ -----------------------------------------------------------------


def _literal_all(tree: ast.Module) -> frozenset[str] | None:
    """The module's literal `__all__` names, or None when it has none (or a
    non-literal one — which falls back to the "otherwise" surface, not an error)."""
    for stmt in tree.body:
        target: ast.expr | None = None
        value: ast.expr | None = None
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
            target, value = stmt.targets[0], stmt.value
        elif isinstance(stmt, ast.AnnAssign):
            target, value = stmt.target, stmt.value
        if isinstance(target, ast.Name) and target.id == "__all__" and value is not None:
            if not isinstance(value, ast.List | ast.Tuple):
                return None
            try:
                names = ast.literal_eval(value)
            except _LITERAL_ERRORS:
                return None
            if isinstance(names, list | tuple) and all(isinstance(n, str) for n in names):
                return frozenset(names)
            return None
    return None


# --- imports (for explicit re-exports) ------------------------------------------


def _relative_base(node: ast.ImportFrom, rel_path: str) -> str:
    """The dotted module this `from` import reads from, resolving `.`/`..` the way
    Python does: a package's own `__init__.py` is its own anchor at level 1."""
    if node.level == 0:
        return node.module or ""
    package = rel_path.removesuffix(".py").split("/")[:-1]
    if node.level - 1 > len(package):
        return ""
    anchor = package[: len(package) - (node.level - 1)]
    return ".".join([*anchor, *([node.module] if node.module else [])])


def _collect_imports(tree: ast.Module, rel_path: str) -> tuple[dict[str, str], set[str]]:
    """Top-level import bindings: name -> resolved dotted source, and the subset
    bound through the explicit `as`-same-name re-export idiom."""
    bindings: dict[str, str] = {}
    explicit: set[str] = set()
    for stmt in tree.body:
        if isinstance(stmt, ast.Import):
            for alias in stmt.names:
                bound = alias.asname or alias.name.split(".")[0]
                bindings[bound] = alias.name
                if alias.asname == alias.name:
                    explicit.add(bound)
        elif isinstance(stmt, ast.ImportFrom) and not any(a.name == "*" for a in stmt.names):
            base = _relative_base(stmt, rel_path)
            for alias in stmt.names:
                bound = alias.asname or alias.name
                bindings[bound] = f"{base}.{alias.name}" if base else alias.name
                if alias.asname == alias.name:
                    explicit.add(bound)
    return bindings, explicit


# --- function / class rendering -------------------------------------------------


def _decorators(
    node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef, indent: str
) -> list[str]:
    return [f"{indent}@{ast.unparse(d)}" for d in node.decorator_list]


def _function_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    prefix = "async " if isinstance(node, ast.AsyncFunctionDef) else ""
    returns = f" -> {ast.unparse(node.returns)}" if node.returns is not None else ""
    return f"{prefix}def {node.name}({ast.unparse(node.args)}){returns}"


def _render_function(node: ast.FunctionDef | ast.AsyncFunctionDef, indent: str) -> list[str]:
    return [*_decorators(node, indent), f"{indent}{_function_signature(node)}"]


def _class_header(node: ast.ClassDef) -> str:
    parts = [ast.unparse(base) for base in node.bases]
    parts += [
        f"**{ast.unparse(kw.value)}" if kw.arg is None else f"{kw.arg}={ast.unparse(kw.value)}"
        for kw in node.keywords
    ]
    return f"class {node.name}({', '.join(parts)})" if parts else f"class {node.name}"


def _class_members(node: ast.ClassDef, indent: str) -> list[tuple[str, list[str]]]:
    members: dict[str, list[str]] = {}
    for stmt in node.body:
        if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef):
            if stmt.name in _ALLOWED_DUNDERS or not stmt.name.startswith("_"):
                members[stmt.name] = _render_function(stmt, indent)
        elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            name = stmt.target.id
            if not name.startswith("_"):
                # Default presence is contract (dataclass/NamedTuple/TypedDict/pydantic
                # required-vs-optional) — same rule as a module-level AnnAssign.
                members[name] = [f"{indent}{_render_var(name, stmt.annotation, stmt.value)}"]
        elif isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                if isinstance(target, ast.Name) and not target.id.startswith("_"):
                    value = _value_suffix(stmt.value, None)
                    members[target.id] = [f"{indent}{target.id} = {value}"]
        elif isinstance(stmt, ast.ClassDef) and not stmt.name.startswith("_"):
            members[stmt.name] = _render_class(stmt, indent)
    return sorted(members.items())


def _render_class(node: ast.ClassDef, indent: str) -> list[str]:
    lines = [*_decorators(node, indent), f"{indent}{_class_header(node)}"]
    for _, member_lines in _class_members(node, indent + "  "):
        lines.extend(member_lines)
    return lines


def _render_var(name: str, annotation: ast.expr | None, value: ast.expr | None) -> str:
    """A variable (module-level, or a class `AnnAssign` field): `NAME[: annotation]
    [ = value-or-...]`. A missing value renders bare — for a field, that means no
    default, i.e. the constructor argument is required."""
    head = f"{name}: {ast.unparse(annotation)}" if annotation is not None else name
    return head if value is None else f"{head} = {_value_suffix(value, annotation)}"


# --- one module's public entries ------------------------------------------------


def _record_module_assign(
    entries: dict[str, list[str]], stmt: ast.Assign, included: Callable[[str], bool]
) -> None:
    for target in stmt.targets:
        if isinstance(target, ast.Name) and target.id != "__all__" and included(target.id):
            entries[target.id] = [_render_var(target.id, None, stmt.value)]


def _record_module_annassign(
    entries: dict[str, list[str]], stmt: ast.AnnAssign, included: Callable[[str], bool]
) -> None:
    if not isinstance(stmt.target, ast.Name):
        return
    name = stmt.target.id
    if name != "__all__" and included(name):
        entries[name] = [_render_var(name, stmt.annotation, stmt.value)]


def _module_defs(tree: ast.Module, included: Callable[[str], bool]) -> dict[str, list[str]]:
    """Public top-level `def`/`class`/`Assign`/`AnnAssign`/`type` entries."""
    entries: dict[str, list[str]] = {}
    for stmt in tree.body:
        if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef) and included(stmt.name):
            entries[stmt.name] = _render_function(stmt, "")
        elif isinstance(stmt, ast.ClassDef) and included(stmt.name):
            entries[stmt.name] = _render_class(stmt, "")
        elif isinstance(stmt, ast.Assign):
            _record_module_assign(entries, stmt, included)
        elif isinstance(stmt, ast.AnnAssign):
            _record_module_annassign(entries, stmt, included)
        elif isinstance(stmt, ast.TypeAlias) and included(stmt.name.id):
            entries[stmt.name.id] = [ast.unparse(stmt)]
    return entries


def _module_reexports(
    tree: ast.Module, rel_path: str, all_names: frozenset[str] | None, taken: set[str]
) -> dict[str, list[str]]:
    """Import-bound public re-exports not already covered by a `def`/`class`/`Assign`."""
    bindings, explicit = _collect_imports(tree, rel_path)
    entries: dict[str, list[str]] = {}
    for name, resolved in bindings.items():
        if name in taken:
            continue
        eligible = (
            name in all_names
            if all_names is not None
            else name in explicit and not name.startswith("_")
        )
        if eligible:
            entries[name] = [f"{name}  (re-export of {resolved})"]
    return entries


def _module_entries(tree: ast.Module, rel_path: str) -> list[tuple[str, list[str]]]:
    all_names = _literal_all(tree)

    def included(name: str) -> bool:
        return name in all_names if all_names is not None else not name.startswith("_")

    entries = _module_defs(tree, included)
    entries.update(_module_reexports(tree, rel_path, all_names, set(entries)))
    return sorted(entries.items())


# --- door rendering --------------------------------------------------------------


def render_door(dotted_door: str, source: str, repo_root: Path) -> str:
    sections: list[tuple[str, str]] = []
    for rel in _door_modules(source, repo_root):
        dotted = _dotted_name(rel)
        tree = ast.parse((repo_root / rel).read_text(encoding="utf-8"), filename=rel)
        lines = [f"## {dotted}"]
        for _, entry_lines in _module_entries(tree, rel):
            lines.extend(entry_lines)
        sections.append((dotted, "\n".join(lines)))
    sections.sort(key=lambda item: item[0])
    header = (
        f"# Public API of {dotted_door} — generated by scripts/structure/contracts.py; "
        f"do not edit (regenerate with {_COMMAND})."
    )
    return "\n\n".join([header, *(text for _, text in sections)]) + "\n"


def _rendered_snapshots(repo_root: Path, doors: tuple[tuple[str, str, str], ...]) -> dict[str, str]:
    return {snapshot: render_door(door, source, repo_root) for door, source, snapshot in doors}


def write_snapshots(
    repo_root: Path | None = None, doors: tuple[tuple[str, str, str], ...] | None = None
) -> None:
    root = _REPO_ROOT if repo_root is None else repo_root
    for snapshot, content in _rendered_snapshots(root, DOORS if doors is None else doors).items():
        (root / snapshot).write_text(content, encoding="utf-8")


def check_snapshots(
    repo_root: Path | None = None, doors: tuple[tuple[str, str, str], ...] | None = None
) -> list[str]:
    """One unified diff per stale (or missing) snapshot; empty when every snapshot
    matches its door's current public surface. `repo_root`/`doors` default to the
    module globals, read at call time — not bound as defaults — so a test can
    monkeypatch `contracts._REPO_ROOT` / `contracts.DOORS` and call `main()`."""
    root = _REPO_ROOT if repo_root is None else repo_root
    use_doors = DOORS if doors is None else doors
    diffs: list[str] = []
    for snapshot, content in _rendered_snapshots(root, use_doors).items():
        path = root / snapshot
        current = path.read_text(encoding="utf-8") if path.exists() else None
        if current == content:
            continue
        diffs.append(
            "".join(
                difflib.unified_diff(
                    (current or "").splitlines(keepends=True),
                    content.splitlines(keepends=True),
                    fromfile=f"a/{snapshot}",
                    tofile=f"b/{snapshot}",
                )
            )
        )
    return diffs


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if "--check" in argv:
        diffs = check_snapshots()
        for diff in diffs:
            print(diff, end="")
        if diffs:
            print(f"\n{len(diffs)} stale contract snapshot(s) — regenerate with `{_COMMAND}`.")
            return 1
        return 0
    if "--write" in argv:
        write_snapshots()
        return 0
    print("usage: contracts.py --write | --check", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
