"""Forbid the wall-clock time-bomb: exact-equality test assertions on
values derived from a fixed instant while the derivation can reach the real
clock.

Run: `.venv/bin/python scripts/lint_time_bomb.py [path ...]` (defaults to the
source dirs + tests/). Also run automatically via pre-commit.

## Why

A fixed-instant constant (`INDEX_LABEL_CUTOVER_AT = datetime(2026, 8, 23, 11, 0,
tzinfo=UTC)`) that production folds against the *real* clock makes every
window-boundary result a function of the wall clock. A test that asserts that
result with exact equality (`assert lifecycle_call["from_"] ==
INDEX_LABEL_CUTOVER_AT`) is correct only while `now` keeps a particular
relation to the constant — and that relation expires the moment the wall clock
passes the constant's cutoff (2026-08-30: two fixed-instant tests went
deterministically red within seven days of each other, each red run ejecting
the whole merge-queue batch). The fix pattern is to *pin* the clock (pass
`now=`/`at=`), assert with a tolerance (`pytest.approx`), or assert the
terminal monotone behavior — never an exact value whose correctness depends on
an unstated wall-clock relation.

## The rules

Two checks, both AST-based (no imports of app code; runs anywhere the source
tree is present — the same zero-dependency shape as the other `scripts/`
lints):

1. **Clock-threading into the fixed-instant world (source).** An in-repo
   function that *accepts* a clock parameter (`now`, `now_utc`, `at`,
   `as_of`, `clock`, `when`, `timestamp`, `instant`) must not call a
   real-now-using function whose clock cannot be reached while the clock
   parameter is live: every call in its body to a callee that
   (transitively) reaches a fixed-instant constant module and uses the real
   clock must either thread the callee's clock parameter or sit inside the
   caller's `param is None -> real now` fallback. Calling
   `split_index_label_window(start, end)` (no `now=`) from inside
   `compute_rollup(now_utc=...)` is exactly the 2026-08-30 rollup bomb's
   seedling: the parameter is a promise the code does not keep, so the test
   that "pins" `now_utc` is still asserting against the real clock.

2. **Exact equality on a fixed instant with an unpinned real-now
   derivation (test).** In a test function, an exact `==`/`!=` comparison
   whose compared expression references a repo fixed-instant constant (or a
   local derived from one) is a time bomb when the same function's value
   derivation can reach the real clock — via `datetime.now`/`time.time`, an
   in-repo call whose clock is not pinned (`retention_floor()` without
   `now=`), or an opaque HTTP call (`client.get(...)`) whose internals the
   linter cannot audit. Pinning is recognized when the callee's clock
   parameter is passed a fixed-instant-derived expression *and* the
   callee's own clock parameter actually guards its real-now paths
   (summary computed by rule 1). A tolerance (`pytest.approx`, or
   `abs(...) < n`) is always allowed. Deliberate exceptions carry an
   inline `# time-bomb-ok: <reason>` comment on the asserted line.

Scope: rule 1 scans non-test source dirs only; rule 2 scans `tests/` only.
Error format `file:line: <reason>` + non-zero exit.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent

_SCAN_DIRS = (
    "agent",
    "ava",
    "ava_builtins",
    "cli",
    "gateway",
    "ops",
    "scripts",
    "services",
    "shared",
)

# A parameter carrying any of these names is treated as the caller-visible
# logical clock (a `now`/`at` the caller can pin). `deadline` is deliberately
# absent: it is a monotonic budget, not a wall-clock instant.
_CLOCK_PARAMS = frozenset(
    {"now", "now_utc", "at", "as_of", "clock", "when", "timestamp", "instant"}
)

# Real-clocks. `monotonic` is deliberately absent: durations measured against
# it are independent of the wall-clock relation that makes a fixed-instant
# window boundary a time bomb.
_REAL_NOW_ATTRS = frozenset({"now", "utcnow", "today"})

_HTTP_NAMES = frozenset({"client", "session", "httpx", "requests"})
_HTTP_METHODS = frozenset({"get", "post", "put", "delete", "request", "patch"})

_OPT_OUT = "time-bomb-ok"


def _is_dt_ctor(node: ast.AST) -> bool:
    """`datetime(...)` / `date(...)` constructor call (incl. `datetime.datetime`)."""
    if isinstance(node, ast.Call):
        f = node.func
        if isinstance(f, ast.Name) and f.id in ("datetime", "date"):
            return True
        if (
            isinstance(f, ast.Attribute)
            and isinstance(f.value, ast.Name)
            and f.value.id == "datetime"
            and f.attr in ("datetime", "date")
        ):
            return True
    return False


class _Index:
    """One parsed view of the repo: modules, fixed-instant constants, and
    lazily-computed per-function summaries."""

    def __init__(self, root: Path, dirs: tuple[str, ...]) -> None:
        self.root = root
        self.trees: dict[str, ast.Module] = {}  # dotted module -> tree
        self.fns: dict[
            str,
            dict[str, tuple[ast.FunctionDef | ast.AsyncFunctionDef, frozenset[str]]],
        ] = {}
        self.fixed: dict[str, frozenset[str]] = {}  # module -> fixed-instant names
        self._summary: dict[tuple[str, str], tuple[bool, bool, bool]] = {}
        self._scan(dirs)

    def _scan(self, dirs: tuple[str, ...]) -> None:
        for d in (*dirs, "tests"):
            for path in self.root.joinpath(d).rglob("*.py"):
                rel = path.relative_to(self.root).as_posix()
                if "__pycache__" in rel or "/mirrors/" in rel:
                    continue
                try:
                    tree = ast.parse(path.read_text(encoding="utf-8"))
                except SyntaxError:
                    continue
                mod = rel[:-3].replace("/", ".")
                self.trees[mod] = tree
                for node in tree.body:
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        params = {
                            a.arg
                            for a in (
                                *node.args.posonlyargs,
                                *node.args.args,
                                *node.args.kwonlyargs,
                            )
                        }
                        self.fns.setdefault(mod, {})[node.name] = (node, frozenset(params))
        # fixed-instant module-level constants + transitive aliases
        for mod, tree in self.trees.items():
            assigns: list[tuple[str | None, ast.AST | None]] = []
            for node in tree.body:
                if isinstance(node, ast.Assign):
                    for target in node.targets:
                        if isinstance(target, ast.Name):
                            assigns.append((target.id, node.value))
                elif (
                    isinstance(node, ast.AnnAssign)
                    and isinstance(node.target, ast.Name)
                    and node.value is not None
                ):
                    assigns.append((node.target.id, node.value))
            names: set[str] = set()
            for name, value in assigns:
                if name is not None and value is not None and _is_dt_ctor(value):
                    names.add(name)
            changed = True
            while changed:
                changed = False
                for name, value in assigns:
                    if name is None or name in names or value is None:
                        continue
                    if any(isinstance(n, ast.Name) and n.id in names for n in ast.walk(value)):
                        names.add(name)
                        changed = True
            if names:
                self.fixed[mod] = frozenset(names)

    def family_modules(self) -> frozenset[str]:
        """Modules that define a fixed-instant constant — the boundary family
        whose window result is a function of a fixed instant."""
        return frozenset(self.fixed)

    def _imports(self, mod: str, name: str) -> tuple[str, str] | None:
        tree = self.trees.get(mod)
        if tree is None:
            return None
        mod_parts = mod.split(".")
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and node.module is not None:
                if node.module.startswith("."):
                    level = len(node.module) - len(node.module.lstrip("."))
                    base = mod_parts[: len(mod_parts) - (level - 1)]
                    cand = ".".join((*base, *node.module.lstrip(".").split(".")))
                else:
                    cand = node.module
                if cand in self.trees:
                    for alias in node.names:
                        if alias.name == name:
                            return cand, alias.name
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    leaf = alias.name.split(".")[-1]
                    if name in (alias.asname, leaf) and alias.name in self.trees:
                        return alias.name, leaf
        return None

    def _resolve(self, mod: str, call: ast.Call) -> tuple[str, str] | None:
        f = call.func
        if isinstance(f, ast.Name):
            if f.id in self.fns.get(mod, {}):
                return mod, f.id
            return self._imports(mod, f.id)
        if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
            direct = self._imports(mod, f.value.id)
            if direct is not None:
                if f.attr in self.fns.get(direct[0], {}):
                    return direct[0], f.attr
                sub = f"{direct[0]}.{direct[1]}"
                if sub in self.fns and f.attr in self.fns[sub]:
                    return sub, f.attr
        return None

    def _inside_fallback(
        self, fn_node: ast.AST, target: ast.AST, clock_params: frozenset[str]
    ) -> bool:
        """`target` sits inside an `if param ...` / `param or ...` / `param if ...`
        shape whose condition references one of the function's clock params —
        the `param is None -> real now` fallback pattern."""
        ancestry: list[ast.AST] = []

        def walk(node: ast.AST) -> bool:
            if node is target:
                return True
            for child in ast.iter_child_nodes(node):
                if walk(child):
                    ancestry.append(node)
                    return True
            return False

        walk(fn_node)
        for anc in ancestry:
            if isinstance(anc, ast.If):
                test: ast.AST | None = anc.test
            elif isinstance(anc, ast.IfExp):
                test = anc.test
            elif isinstance(anc, ast.BoolOp):
                for value in anc.values:
                    refs = [n.id for n in ast.walk(value) if isinstance(n, ast.Name)]
                    if any(r in clock_params for r in refs):
                        return True
                test = None
            else:
                test = None
            if test is not None:
                refs = [n.id for n in ast.walk(test) if isinstance(n, ast.Name)]
                if any(r in clock_params for r in refs):
                    return True
        return False

    def _call_threaded(
        self, call: ast.Call, callee: tuple[str, str], caller_clock: frozenset[str]
    ) -> bool:
        mod, name = callee
        info = self.fns.get(mod, {}).get(name)
        if info is None:
            return False
        node, params = info
        cclock = params & _CLOCK_PARAMS
        if not cclock:
            return False
        all_params = [p.arg for p in node.args.posonlyargs + node.args.args]
        supplied: set[str] = set()
        for i in range(min(len(call.args), len(all_params))):
            if all_params[i] in cclock:
                supplied.add(all_params[i])
        for kw in call.keywords:
            if kw.arg in cclock:
                supplied.add(kw.arg)
        if not cclock <= supplied:
            return False

        # every supplied clock arg must be pin-safe: reference the caller's
        # clock param (threaded) or contain no real-now sink (a fixed literal).
        def pin_safe(expr: ast.AST) -> bool:
            for n in ast.walk(expr):
                if isinstance(n, ast.Name) and n.id in caller_clock:
                    return True
                if (
                    isinstance(n, ast.Call)
                    and isinstance(n.func, ast.Attribute)
                    and isinstance(n.func.value, ast.Name)
                    and n.func.value.id in ("datetime", "time")
                    and n.func.attr in _REAL_NOW_ATTRS
                ):
                    return False
            return True

        for i in range(min(len(call.args), len(all_params))):
            if all_params[i] in cclock and not pin_safe(call.args[i]):
                return False
        return all(pin_safe(kw.value) for kw in call.keywords if kw.arg in cclock)

    def summary(
        self, mod: str, name: str, depth: int = 0, seen: frozenset | None = None
    ) -> tuple[bool, bool, bool]:
        """(uses_real_now, clock_covered, reaches_family) — transitive, memoized.

        `clock_covered`: every real-now path is guarded by the function's own
        clock parameter (the None-fallback) or threaded into a callee that is
        covered itself. A function with no clock parameter has clock_covered
        False whenever it uses the real clock (its clock cannot be pinned).
        """
        key = (mod, name)
        cached = self._summary.get(key)
        if cached is not None:
            return cached
        info = self.fns.get(mod, {}).get(name)
        if info is None:
            return (False, True, False)
        node, params = info
        clock = params & _CLOCK_PARAMS
        visited: set[tuple[str, str]] = set(seen) if seen is not None else set()
        if key in visited:
            return (False, True, False)
        visited = visited | {key}
        uses = False
        covered = True
        family = mod in self.family_modules()
        for sub in ast.walk(node):
            if (
                isinstance(sub, ast.Call)
                and isinstance(sub.func, ast.Attribute)
                and isinstance(sub.func.value, ast.Name)
                and sub.func.value.id in ("datetime", "time")
                and sub.func.attr in _REAL_NOW_ATTRS
            ):
                uses = True
                if not clock or not self._inside_fallback(node, sub, clock):
                    covered = False
            if isinstance(sub, ast.Call):
                resolved = self._resolve(mod, sub)
                if resolved is None:
                    continue
                cmod, cname = resolved
                cuses, ccovered, cfam = self.summary(cmod, cname, depth + 1, frozenset(visited))
                if cuses:
                    uses = True
                    family = family or cfam
                    if self._inside_fallback(node, sub, clock):
                        continue
                    if not (ccovered and self._call_threaded(sub, (cmod, cname), clock)):
                        covered = False
        self._summary[key] = (uses, covered, family)
        return (uses, covered, family)


# ── rule 1: clock-threading into the fixed-instant world (source) ────────────

_TEST_PATTERNS = (
    re.compile(r"(^|/)tests?/"),
    re.compile(r"(^|/)test_[^/]+\.py$"),
    re.compile(r"_test\.py$"),
)


def _is_test_path(rel: str) -> bool:
    return any(pat.search(rel) for pat in _TEST_PATTERNS)


def _lint_source(index: _Index, paths: list[Path]) -> list[str]:
    errors: list[str] = []
    for path in paths:
        if path.is_dir():
            for p in sorted(path.rglob("*.py")):
                rel = p.relative_to(_REPO_ROOT).as_posix()
                if _is_test_path(rel):
                    continue
                errors.extend(_lint_source_file(index, p, rel))
        elif path.suffix == ".py":
            rel = path.relative_to(_REPO_ROOT).as_posix()
            if not _is_test_path(rel):
                errors.extend(_lint_source_file(index, path, rel))
    return errors


def _lint_source_file(index: _Index, path: Path, rel: str) -> list[str]:
    mod = rel[:-3].replace("/", ".")
    errors: list[str] = []
    fns = index.fns.get(mod, {})
    for name, (fn_node, params) in fns.items():
        clock = params & _CLOCK_PARAMS
        if not clock:
            continue
        uses, covered, family = index.summary(mod, name)
        if not (uses and not covered and family):
            continue
        seen: set[tuple[str, str]] = set()
        for sub in ast.walk(fn_node):
            if not isinstance(sub, ast.Call):
                continue
            resolved = index._resolve(mod, sub)
            if resolved is None:
                continue
            cmod, cname = resolved
            cuses, ccovered, cfam = index.summary(cmod, cname)
            if not (cuses and cfam):
                continue
            if index._inside_fallback(fn_node, sub, clock):
                continue
            if ccovered and index._call_threaded(sub, (cmod, cname), clock):
                continue
            if (cmod, cname) in seen:
                continue
            seen.add((cmod, cname))
            errors.append(
                f"{path}:{sub.lineno}: {name} accepts a clock parameter "
                f"({', '.join(sorted(clock))}) but calls {cmod}.{cname} — which "
                "uses the real clock against a fixed-instant boundary — without "
                "threading it; pass the clock through (now=.../at=...) so tests "
                "can pin the window instead of riding the wall clock"
            )
    return errors


# ── rule 2: exact equality on a fixed instant with an unpinned derivation ────


def _lint_tests(index: _Index, paths: list[Path]) -> list[str]:
    errors: list[str] = []
    for path in paths:
        if path.is_dir():
            for p in sorted(path.rglob("*.py")):
                rel = p.relative_to(_REPO_ROOT).as_posix()
                if _is_test_path(rel):
                    errors.extend(_lint_test_file(index, p, rel))
        elif path.suffix == ".py":
            rel = path.relative_to(_REPO_ROOT).as_posix()
            if _is_test_path(rel):
                errors.extend(_lint_test_file(index, path, rel))
    return errors


def _fixed_names_in_module(index: _Index, tree: ast.Module) -> tuple[set[str], dict[str, str]]:
    """(bare fixed-instant names imported, module-alias -> dotted module)."""
    names: set[str] = set()
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module is not None:
            cand = node.module
            if cand in index.fixed:
                for alias in node.names:
                    if alias.name in index.fixed[cand]:
                        names.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                leaf = alias.name.split(".")[-1]
                if alias.name in index.fixed:
                    aliases[alias.asname or leaf] = alias.name
    return names, aliases


def _local_derives(
    function: ast.AST, fixed_names: set[str], aliases: dict[str, str], index: _Index
) -> tuple[set[str], set[str]]:
    """(fixed-derived local names, real-now-derived local names)."""
    fixed: set[str] = set()
    real: set[str] = set()

    def refs_fixed(node: ast.AST) -> bool:
        for n in ast.walk(node):
            if isinstance(n, ast.Name) and n.id in (fixed_names | fixed):
                return True
            if (
                isinstance(n, ast.Attribute)
                and isinstance(n.value, ast.Name)
                and n.value.id in aliases
                and n.attr in index.fixed.get(aliases[n.value.id], ())
            ):
                return True
        return False

    def is_real(node: ast.AST) -> bool:
        for n in ast.walk(node):
            if (
                isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute)
                and isinstance(n.func.value, ast.Name)
                and n.func.value.id in ("datetime", "time")
                and n.func.attr in _REAL_NOW_ATTRS
            ):
                return True
        return False

    for sub in ast.walk(function):
        if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)) and sub is not function:
            continue  # nested helper scopes are not tracked here
        if isinstance(sub, ast.Assign):
            for target in sub.targets:
                if isinstance(target, ast.Name):
                    if is_real(sub.value):
                        real.add(target.id)
                        fixed.discard(target.id)
                    elif refs_fixed(sub.value):
                        fixed.add(target.id)
                        real.discard(target.id)
    return fixed, real


def _expr_refs_fixed(
    expr: ast.AST,
    fixed_names: set[str],
    local_fixed: set[str],
    aliases: dict[str, str],
    index: _Index,
) -> bool:
    for n in ast.walk(expr):
        if isinstance(n, ast.Name) and n.id in (fixed_names | local_fixed):
            return True
        if (
            isinstance(n, ast.Attribute)
            and isinstance(n.value, ast.Name)
            and n.value.id in aliases
            and n.attr in index.fixed.get(aliases[n.value.id], ())
        ):
            return True
    return False


def _tainted(function: ast.AST, index: _Index, mod: str) -> list[str]:
    """Real-now contamination sources in one test function body."""
    taints: list[str] = []
    for sub in ast.walk(function):
        if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)) and sub is not function:
            continue
        if not isinstance(sub, ast.Call):
            continue
        f = sub.func
        if (
            isinstance(f, ast.Attribute)
            and isinstance(f.value, ast.Name)
            and f.value.id in _HTTP_NAMES
            and f.attr in _HTTP_METHODS
        ):
            taints.append(f"{f.value.id}.{f.attr} (opaque HTTP)")
            continue
        if isinstance(f, ast.Name) and f.id == "TestClient":
            taints.append("TestClient (opaque HTTP)")
            continue
        if (
            isinstance(f, ast.Attribute)
            and isinstance(f.value, ast.Name)
            and f.value.id in ("datetime", "time")
            and f.attr in _REAL_NOW_ATTRS
        ):
            taints.append(f"{f.value.id}.{f.attr}")
            continue
        resolved = index._resolve(mod, sub)
        if resolved is None:
            continue
        cuses, ccovered, _cfam = index.summary(*resolved)
        if not cuses:
            continue
        if ccovered and index._call_threaded(sub, resolved, frozenset()):
            continue
        taints.append(f"{resolved[0]}.{resolved[1]} (clock not pinned)")
    return taints


def _lint_test_file(index: _Index, path: Path, rel: str) -> list[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:
        return []
    mod = rel[:-3].replace("/", ".")
    fixed_names, aliases = _fixed_names_in_module(index, tree)
    if not fixed_names and not aliases:
        return []  # nothing fixed-instant in this test module — fast path
    errors: list[str] = []
    source_lines = path.read_text(encoding="utf-8").splitlines()
    for node in tree.body:
        if not isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef)
        ) or not node.name.startswith("test_"):
            continue
        fixed_local, _real_local = _local_derives(node, fixed_names, aliases, index)
        taints = _tainted(node, index, mod)
        if not taints:
            continue
        for sub in ast.walk(node):
            if isinstance(sub, ast.Compare) and any(
                isinstance(op, (ast.Eq, ast.NotEq)) for op in sub.ops
            ):
                sides = [sub.left, *sub.comparators]
                if any(
                    _expr_refs_fixed(s, fixed_names, fixed_local, aliases, index) for s in sides
                ):
                    lines = source_lines[sub.lineno - 1 : sub.end_lineno]
                    if any(_OPT_OUT in line for line in lines):
                        continue
                    errors.append(
                        f"{path}:{sub.lineno}: time-bomb test: exact equality "
                        "on a value derived from a fixed instant while the "
                        f"derivation can reach the real clock ({'; '.join(taints)}); "
                        "pin the clock (pass now=...), assert with a tolerance, or "
                        f"add '# {_OPT_OUT}: <reason>' to opt out"
                    )
    return errors


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    root = _REPO_ROOT
    if argv:
        paths = [Path(a) for a in argv]
        used = {p for p in paths if p.is_dir()}
        dirs = tuple(d for d in _SCAN_DIRS if (root / d).is_dir()) if used else ()
        index = _Index(root, dirs)
        errors = _lint_source(index, paths) + _lint_tests(index, paths)
    else:
        dirs = tuple(d for d in _SCAN_DIRS if (root / d).is_dir())
        index = _Index(root, dirs)
        errors = _lint_source(index, [root / d for d in dirs]) + _lint_tests(
            index, [root / "tests"]
        )
    for err in errors:
        print(err, file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
