"""Path imports: code under ava_builtins/ reaches other code through packages, never a file path.

A skill or plugin script that edits `sys.path`, calls `site.addsitedir`, or loads
a module from a file (`importlib.util.spec_from_file_location`,
`importlib.machinery.SourceFileLoader`, `runpy.run_path`) turns its directory into
an unreviewed code package that sidesteps the package doors, budgets and locality
rules. Shared code belongs in a governed package the script imports normally, and
the script stays a thin entry point. Files under a `tests/` directory are exempt.
Rule 6 in scripts/lint/code_structure.py rejects every measured site directly;
there is no baseline allowance.

**One narrow exception** (2026-09-29 narrowing): a script under
`ava_builtins/skills/<group>/<skill>/` may run a one-line `sys.path.insert(0, ...)` /
`sys.path.append(...)` guard whose argument is derived from `__file__` and whose
resolved directory stays inside that same `<skill>/` tree — the pattern
`lint_no_script_sibling_imports.py` already documents
(`sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))`, or the
`pathlib` equivalent `str(Path(__file__).resolve().parent)`), optionally with a
`/ "subdir"` (or `os.path.join(..., "subdir")`) tail to reach a sibling
sub-skill's own `scripts/` directory. `measure()` recognizes this shape and does
not count it as a site at all — it is not debt to freeze, it is the endorsed way
for a skill's scripts to import their own siblings. Three things still always
count as a site, with no exception:

- a `sys.path` mutation whose target resolves outside the script's own
  top-level skill directory (a cross-skill reach, or anything under
  `ava_builtins/plugins/` or another non-skill tree);
- any of the four file-loader calls (`importlib.util.spec_from_file_location`,
  `importlib.machinery.SourceFileLoader`, `runpy.run_path`, `site.addsitedir`),
  even with a `__file__`-derived, in-skill argument — a loader is a package-door
  bypass regardless of where it points;
- any other `sys.path` mutation form (`extend`/`remove`/`pop`/`clear`, a slice
  assignment, a plain assignment) — only a literal `.insert(0, ...)` or
  `.append(...)` guard is recognized.
"""

from __future__ import annotations

import ast

from scripts.structure import lint_common

SECTION = "path_imports"
_SCOPE = ("ava_builtins/",)
_SKILLS_SCOPE = "ava_builtins/skills/"
FIX = (
    "move the shared code into a governed package (ava/, base/, or the plugin's own "
    "package) and import it normally; keep the script a thin entry point"
)
_PATH_MUTATORS = frozenset({"insert", "append", "extend", "remove", "pop", "clear"})
# Loader callable -> its measured site key.
_LOADERS = {
    "spec_from_file_location": "importlib.util.spec_from_file_location",
    "SourceFileLoader": "importlib.machinery.SourceFileLoader",
    "run_path": "runpy.run_path",
    "addsitedir": "site.addsitedir",
}
Sites = dict[str, list[int]]


class _Bindings:
    """Local names bound to the sys module, to `sys.path` itself, and to a path loader."""

    def __init__(self, tree: ast.Module) -> None:
        self.sys_names: set[str] = set()
        self.path_names: set[str] = set()
        self.loaders: dict[str, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                self.sys_names.update(
                    alias.asname or alias.name for alias in node.names if alias.name == "sys"
                )
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                for alias in node.names:
                    local = alias.asname or alias.name
                    if node.module == "sys" and alias.name == "path":
                        self.path_names.add(local)
                    elif alias.name in _LOADERS:
                        self.loaders[local] = _LOADERS[alias.name]

    def is_sys_path(self, node: ast.expr) -> bool:
        if isinstance(node, ast.Subscript):
            node = node.value
        if isinstance(node, ast.Name):
            return node.id in self.path_names
        return (
            isinstance(node, ast.Attribute)
            and node.attr == "path"
            and isinstance(node.value, ast.Name)
            and node.value.id in self.sys_names
        )

    def call_target(self, call: ast.Call) -> str | None:
        func = call.func
        if isinstance(func, ast.Name):
            return self.loaders.get(func.id)
        if not isinstance(func, ast.Attribute):
            return None
        if func.attr in _PATH_MUTATORS and self.is_sys_path(func.value):
            return "sys.path"
        return _LOADERS.get(func.attr)


def _assigned(node: ast.stmt) -> list[ast.expr]:
    if isinstance(node, ast.Assign | ast.Delete):
        return list(node.targets)
    if isinstance(node, ast.AugAssign | ast.AnnAssign):
        return [node.target]
    return []


# ── the allowed within-skill `__file__` guard ──────────────────────────────


def _string_const(node: ast.expr) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _is_file_dunder(node: ast.expr) -> bool:
    return isinstance(node, ast.Name) and node.id == "__file__"


def _strip_call(node: ast.expr, name: str, *, nargs: int) -> ast.expr | None:
    """`name(...)` with exactly `nargs` positional args and no keywords -> its
    first arg. `str(X)`, `X.resolve()` (nargs=0, attribute call) and
    `os.path.abspath(X)` all fit this shape via different callers below."""
    if not (isinstance(node, ast.Call) and not node.keywords and len(node.args) == nargs):
        return None
    func = node.func
    if isinstance(func, ast.Name) and func.id == name:
        return node.args[0] if nargs else node  # bare-name call: str(X)
    if isinstance(func, ast.Attribute) and func.attr == name:
        return node.args[0] if nargs else func.value  # X.resolve() / os.path.dirname(X)
    return None


def _strip_str(node: ast.expr) -> ast.expr:
    return _strip_call(node, "str", nargs=1) or node


def _strip_resolve(node: ast.expr) -> ast.expr:
    return _strip_call(node, "resolve", nargs=0) or node


def _strip_abspath(node: ast.expr) -> ast.expr:
    return _strip_call(node, "abspath", nargs=1) or node


def _parent_chain(node: ast.expr) -> tuple[ast.expr, int]:
    """Walk a `.parent` attribute chain (with `.resolve()` calls interleaved
    anywhere in it), returning (base_expr, how many `.parent` hops)."""
    count = 0
    while True:
        node = _strip_resolve(node)
        if isinstance(node, ast.Attribute) and node.attr == "parent":
            count += 1
            node = node.value
            continue
        return node, count


def _dirname_chain(node: ast.expr) -> tuple[ast.expr, int]:
    """Walk nested `os.path.dirname(...)` calls, returning (innermost_arg, count)."""
    count = 0
    while True:
        inner = _strip_call(node, "dirname", nargs=1)
        if inner is None:
            return node, count
        count += 1
        node = inner


def _split_append(node: ast.expr) -> tuple[ast.expr, list[str] | None]:
    """Split a `<base> / "a" / "b"` BinOp-Div chain, or an
    `os.path.join(<base>, "a", "b")` call, into (base_expr, [components]).
    (node, []) if neither shape applies; (node, None) if either shape applies
    but a component is not a plain string literal (an unrecognizable append)."""
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        parts: list[str] = []
        cur: ast.expr = node
        while isinstance(cur, ast.BinOp) and isinstance(cur.op, ast.Div):
            comp = _string_const(cur.right)
            if comp is None:
                return node, None
            parts.insert(0, comp)
            cur = cur.left
        return cur, parts
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "join"
        and node.args
    ):
        parts = []
        for a in node.args[1:]:
            comp = _string_const(a)
            if comp is None:
                return node, None
            parts.append(comp)
        return node.args[0], parts
    return node, []


def _resolve_file_derived(expr: ast.expr) -> tuple[int, list[str]] | None:
    """Recognize `Path(__file__).resolve().parent...` chains and nested
    `os.path.dirname(os.path.abspath(__file__))` calls, each optionally
    followed by a `/ "sub"` or `os.path.join(..., "sub")` tail. Returns
    `(levels_up, appended_components)` counted from the script's OWN
    directory (0 = own directory, appended=[]), or None when `expr` is not
    one of these two recognized shapes."""
    base, appended = _split_append(_strip_str(expr))
    if appended is None:
        return None
    base = _strip_str(base)
    node, parent_hops = _parent_chain(base)
    if parent_hops:
        # Path(__file__) is the FILE's own path; one `.parent` reaches its
        # directory (0 levels up from "own directory"), each further
        # `.parent` is one more level up.
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "Path"
            and len(node.args) == 1
            and _is_file_dunder(node.args[0])
        ):
            return parent_hops - 1, appended
        return None
    inner, dirname_hops = _dirname_chain(base)
    if dirname_hops:
        if _is_file_dunder(_strip_abspath(inner)):
            return dirname_hops - 1, appended
        return None
    return None


def _guard_target(rel_path: str, expr: ast.expr) -> list[str] | None:
    """The resolved target directory (repo-relative parts) of a recognized
    `__file__`-derived `expr` sited at `rel_path`, or None when `expr` is not
    one of the two recognized shapes, or it resolves above the repo root."""
    resolved = _resolve_file_derived(expr)
    if resolved is None:
        return None
    up, appended = resolved
    file_dir_parts = rel_path.split("/")[:-1]
    if up < 0 or up >= len(file_dir_parts):
        return None
    return file_dir_parts[: len(file_dir_parts) - up] + appended


def _is_within_own_skill(rel_path: str, target_parts: list[str]) -> bool:
    """True when `target_parts` stays inside the top-level skill directory that
    `rel_path` (an `ava_builtins/skills/<group>/<skill>/...` file) itself lives under —
    `ava_builtins/skills/<group>/<skill>/` is the boundary, not the narrower
    directory a nested sub-skill happens to sit in."""
    if not rel_path.startswith(_SKILLS_SCOPE):
        return False
    file_parts = rel_path.split("/")
    if len(file_parts) < 4:
        return False
    skill_root = file_parts[:4]  # ["ava_builtins", "skills", "<group>", "<skill>"]
    return target_parts[: len(skill_root)] == skill_root


def _is_allowed_skill_guard(rel_path: str, call: ast.Call) -> bool:
    """True for a `sys.path.insert(0, <file-derived>)` / `.append(<file-derived>)`
    call at `rel_path` whose argument resolves to a directory inside the same
    top-level skill. Only these two mutators are ever eligible — `extend` /
    `remove` / `pop` / `clear`, and any assignment form, stay violations."""
    if not isinstance(call.func, ast.Attribute):
        return False
    if call.func.attr == "insert":
        if len(call.args) != 2 or call.keywords:
            return False
        if not (isinstance(call.args[0], ast.Constant) and call.args[0].value == 0):
            return False
        arg = call.args[1]
    elif call.func.attr == "append":
        if len(call.args) != 1 or call.keywords:
            return False
        arg = call.args[0]
    else:
        return False
    target = _guard_target(rel_path, arg)
    return target is not None and _is_within_own_skill(rel_path, target)


def measure(tree: ast.Module, rel_path: str) -> Sites:
    """Path-import sites in one module under `ava_builtins/`, keyed `path::target`.

    A test file (under any `tests/` directory) is exempt: a test loads the script
    it proves by path on purpose. Skips a call that `_is_allowed_skill_guard`
    recognizes as the endorsed within-skill `__file__` guard — that shape is not
    a site at all, so it never becomes a violation (see the module
    docstring)."""
    if not rel_path.startswith(_SCOPE) or lint_common.is_test_path(rel_path):
        return {}
    bindings = _Bindings(tree)
    sites: Sites = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and (target := bindings.call_target(node)) is not None:
            if target == "sys.path" and _is_allowed_skill_guard(rel_path, node):
                continue
            sites.setdefault(f"{rel_path}::{target}", []).append(node.lineno)
        elif isinstance(node, ast.stmt) and any(map(bindings.is_sys_path, _assigned(node))):
            sites.setdefault(f"{rel_path}::sys.path", []).append(node.lineno)
    return sites
