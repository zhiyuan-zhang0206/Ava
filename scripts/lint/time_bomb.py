"""Forbid the wall-clock time-bomb: exact-equality test assertions on
values derived from a fixed instant while the derivation can reach the real
clock, and fixed calendar fixtures bound to window-shaped names in tests.

Run: `.venv/bin/python scripts/lint/time_bomb.py [path ...]` (defaults to the
source dirs + tests/; an explicit path that does not exist is an error (stderr +
exit 1) rather than a silent no-op). Also run automatically via pre-commit.

## Why

A fixed-instant constant (`CUTOVER_AT = datetime(2026, 8, 23, 11, 0,
tzinfo=UTC)`) that production folds against the *real* clock makes every
window-boundary result a function of the wall clock. A test that asserts that
result with exact equality (`assert lifecycle_call["from_"] ==
CUTOVER_AT`) is correct only while `now` keeps a particular
relation to the constant — and that relation expires the moment the wall clock
passes the constant's cutoff (2026-08-30: two fixed-instant tests went
deterministically red within seven days of each other, each red run ejecting
the whole merge-queue batch). The fix pattern is to *pin* the clock (pass
`now=`/`at=`), assert with a tolerance (`pytest.approx`), or assert the
terminal monotone behavior — never an exact value whose correctness depends on
an unstated wall-clock relation.

## The rules

Three checks, all AST-based (no imports of app code; runs anywhere the source
tree is present — the same zero-dependency shape as the other `scripts/`
lints):

1. **Clock-threading into the fixed-instant world (source).** An in-repo
   function that *accepts* a clock parameter (`now`, `now_utc`, `at`,
   `as_of`, `clock`, `when`, `timestamp`, `instant`) must not call a
   real-now-using function whose clock cannot be reached while the clock
   parameter is live: every call in its body to a callee that
   (transitively) reaches a fixed-instant constant module and uses the real
   clock must either thread the callee's clock parameter or sit inside the
   caller's `param is None -> real now` fallback. Calling a window helper
   that folds a fixed instant against the real clock from inside
   `compute_rollup(now_utc=...)` without threading the clock is exactly the
   2026-08-30 rollup bomb's seedling: the parameter is a promise the code
   does not keep, so the test that "pins" `now_utc` is still asserting
   against the real clock. (The original seam — the label-window split
   accepting `now=` — has since been removed; the rule stays general.)

2. **Exact equality on a fixed instant with an unpinned real-now
   derivation (test).** In a test function, an exact `==`/`!=` comparison
   whose compared expression references a repo fixed-instant constant (or a
   local derived from one) is a time bomb when the same function's value
   derivation can reach the real clock — via `datetime.now`/`time.time`, an
   in-repo call whose clock is not pinned (a retention floor without
   `now=`), or an opaque HTTP call (`client.get(...)`) whose internals the
   linter cannot audit. Pinning is recognized when the callee's clock
   parameter is passed a fixed-instant-derived expression *and* the
   callee's own clock parameter actually guards its real-now paths
   (summary computed by rule 1). A tolerance (`pytest.approx`, or
   `abs(...) < n`) is always allowed. Deliberate exceptions carry an
   inline `# time-bomb-ok: <reason>` comment on the asserted line.

3. **Fixed calendar fixtures bound to window-shaped names (test).** A fixed
   calendar literal (`"2026-09-06"`, `date(2026, 6, 9)`,
   `datetime(2026, 7, 22, 18, tzinfo=UTC)`) bound to a window-shaped name
   (`day`, `date`, `since`, `until`, `window_start`, `window_end`) as a dict
   value, keyword argument, or plain assignment is a time bomb the moment the
   window it feeds is evaluated against the real clock: the fixture rots the
   day the window rolls past it. Derive the value from the clock (or from the
   request under test), or carry `# time-bomb-ok: <reason>` on the binding.
   Scanned positions are the dict value, keyword argument, and plain
   assignment — attribute/subscript targets, `AnnAssign`, and values laundered
   through other names are deliberately out of scope.

Scope: rule 1 scans non-test source dirs only; rules 2 and 3 scan test files
only (the top-level `tests/` and every package's own `tests/` directory). Error
format `file:line: <reason>` + non-zero exit.
"""

from __future__ import annotations

import ast
import re
import sys
from collections.abc import Callable, Iterator
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from scripts.structure import lint_common  # noqa: E402 - standalone script
from scripts.structure.lazy_modules import ModuleMap  # noqa: E402 - standalone script

_SCAN_DIRS = (*lint_common.FRAMEWORK_DIRS, "scripts")

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


def _is_real_now_call(node: ast.AST) -> bool:
    """`datetime.now()` / `time.time()`-style call on the real clock."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id in ("datetime", "time")
        and node.func.attr in _REAL_NOW_ATTRS
    )


_HTTP_NAMES = frozenset({"client", "session", "httpx", "requests"})
_HTTP_METHODS = frozenset({"get", "post", "put", "delete", "request", "patch"})

_OPT_OUT = "time-bomb-ok"

# Rule 3 window-shaped names: a fixed calendar literal bound to one of these
# is a fixture date that will rot against the real clock.
_WINDOW_NAMES = frozenset({"day", "date", "since", "until", "window_start", "window_end"})
_CALENDAR_STR = re.compile(r"^\d{4}-\d{2}-\d{2}($|[T ])")


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


# A module can only hold a fixed-instant constant (`NAME = datetime(...)` / `date(...)`) if its
# text spells that constructor call, so only those modules are parsed to look for one. The loose
# pattern (it also matches `update(`) is the fast literal scan; the exact one settles its hits.
_FIXED_CTOR_LOOSE = re.compile(r"date(?:time)?\s*\(")
_FIXED_CTOR_HINT = re.compile(r"\b(?:datetime|date)\s*\(")

_Functions = dict[str, tuple[ast.FunctionDef | ast.AsyncFunctionDef, frozenset[str]]]


class _FixedMap(ModuleMap[frozenset[str]]):
    """module -> its fixed-instant names; a module without any is not a member."""

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and self.get(name) is not None


def _top_level_functions(tree: ast.Module) -> _Functions:
    functions: _Functions = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)
            functions[node.name] = (node, frozenset(a.arg for a in args))
    return functions


def _module_assignments(tree: ast.Module) -> list[tuple[str, ast.AST]]:
    """(name, value) of every module-level `NAME = value` / `NAME: T = value`."""
    found: list[tuple[str, ast.AST]] = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            found.extend((t.id, node.value) for t in node.targets if isinstance(t, ast.Name))
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.value is not None
        ):
            found.append((node.target.id, node.value))
    return found


def _fixed_instant_names(tree: ast.Module) -> frozenset[str]:
    """Module-level names bound to a fixed instant, directly or through other such names."""
    assigns = _module_assignments(tree)
    names = {name for name, value in assigns if _is_dt_ctor(value)}
    while derived := {
        name
        for name, value in assigns
        if name not in names
        and any(isinstance(n, ast.Name) and n.id in names for n in ast.walk(value))
    }:
        names |= derived
    return frozenset(names)


class _Index:
    """A view of the repo: modules, fixed-instant constants, and per-function summaries.

    Everything is built on demand: a module is read and parsed the first time a call, an
    import or a judged file resolves into it, so a run that judges a few files touches a few
    modules instead of the repository. A module is anything under the scanned dirs (and
    `tests/`) at the path its dotted name spells.
    """

    def __init__(self, root: Path, dirs: tuple[str, ...]) -> None:
        self.root = root
        self._scanned = frozenset((*dirs, "tests"))
        self._exists: dict[str, bool] = {}
        self._trees: dict[str, ast.Module | None] = {}
        self.trees = ModuleMap(self._is_module, self._tree)
        self.fns = ModuleMap(self._is_module, self._functions)
        self.fixed = _FixedMap(self._is_module, self._fixed_names)  # module -> fixed-instant names
        self._summary: dict[tuple[str, str], tuple[bool, bool, bool]] = {}

    def functions(self, mod: str) -> _Functions:
        """The module's top-level functions; none for an unknown or unparseable module."""
        return self.fns.get(mod) or {}

    def fixed_names(self, mod: str) -> frozenset[str]:
        """The module's fixed-instant names; none when it defines no such constant."""
        return self.fixed.get(mod) or frozenset()

    def _path(self, mod: str) -> Path:
        return self.root / (mod.replace(".", "/") + ".py")

    def _is_module(self, mod: str) -> bool:
        if mod not in self._exists:
            relative = mod.replace(".", "/")
            self._exists[mod] = (
                mod.split(".", 1)[0] in self._scanned
                and "/mirrors/" not in relative
                and "__pycache__" not in relative
                and self._path(mod).is_file()
            )
        return self._exists[mod]

    def _tree(self, mod: str) -> ast.Module | None:
        if mod not in self._trees:
            try:
                self._trees[mod] = ast.parse(self._path(mod).read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, SyntaxError):
                self._trees[mod] = None
        return self._trees[mod]

    def _functions(self, mod: str) -> _Functions | None:
        tree = self._tree(mod)
        return None if tree is None else _top_level_functions(tree)

    def _fixed_names(self, mod: str) -> frozenset[str] | None:
        try:
            text = self._path(mod).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None
        if _FIXED_CTOR_LOOSE.search(text) is None or _FIXED_CTOR_HINT.search(text) is None:
            return None
        tree = self._tree(mod)
        return (_fixed_instant_names(tree) or None) if tree is not None else None

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
            if f.id in self.functions(mod):
                return mod, f.id
            return self._imports(mod, f.id)
        if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
            direct = self._imports(mod, f.value.id)
            if direct is not None:
                if f.attr in self.functions(direct[0]):
                    return direct[0], f.attr
                sub = f"{direct[0]}.{direct[1]}"
                if f.attr in self.functions(sub):
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
            if isinstance(anc, ast.If | ast.IfExp):
                tests: list[ast.AST] = [anc.test]
            elif isinstance(anc, ast.BoolOp):
                tests = list(anc.values)
            else:
                continue
            for test in tests:
                if any(isinstance(n, ast.Name) and n.id in clock_params for n in ast.walk(test)):
                    return True
        return False

    def _call_threaded(
        self, call: ast.Call, callee: tuple[str, str], caller_clock: frozenset[str]
    ) -> bool:
        mod, name = callee
        info = self.functions(mod).get(name)
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
                if _is_real_now_call(n):
                    return False
            return True

        for i in range(min(len(call.args), len(all_params))):
            if all_params[i] in cclock and not pin_safe(call.args[i]):
                return False
        return all(pin_safe(kw.value) for kw in call.keywords if kw.arg in cclock)

    def _walk_summary(
        self,
        mod: str,
        node: ast.AST,
        clock: frozenset[str],
        visited: set[tuple[str, str]],
        depth: int,
    ) -> tuple[bool, bool, bool]:
        """`(uses_real_now, clock_covered, reaches_family)` of one function body."""
        uses = False
        covered = True
        family = mod in self.fixed
        for sub in ast.walk(node):
            if _is_real_now_call(sub):
                uses = True
                covered = covered and bool(clock) and self._inside_fallback(node, sub, clock)
            if isinstance(sub, ast.Call):
                resolved = self._resolve(mod, sub)
                if resolved is None:
                    continue
                cuses, ccovered, cfam = self.summary(*resolved, depth + 1, frozenset(visited))
                if not cuses:
                    continue
                uses = True
                family = family or cfam
                if not self._inside_fallback(node, sub, clock) and not (
                    ccovered and self._call_threaded(sub, resolved, clock)
                ):
                    covered = False
        return uses, covered, family

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
        info = self.functions(mod).get(name)
        if info is None:
            return (False, True, False)
        node, params = info
        clock = params & _CLOCK_PARAMS
        visited: set[tuple[str, str]] = set(seen) if seen is not None else set()
        if key in visited:
            return (False, True, False)
        visited = visited | {key}
        uses, covered, family = self._walk_summary(mod, node, clock, visited, depth)
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


def _rel_or_abs(path: Path) -> str:
    """Repo-relative posix path, or the absolute path for a target outside the repo."""
    return path.as_posix().removeprefix(_REPO_ROOT.as_posix() + "/")


def _lint_source(
    index: _Index, paths: list[Path], scope: frozenset[str] | None = None
) -> list[str]:
    errors: list[str] = []
    for path in paths:
        if path.is_dir():
            for p in sorted(path.rglob("*.py")):
                rel = _rel_or_abs(p)
                if _is_test_path(rel) or (scope is not None and rel not in scope):
                    continue
                errors.extend(_lint_source_file(index, p, rel))
        elif path.suffix == ".py":
            rel = _rel_or_abs(path)
            if not _is_test_path(rel):
                errors.extend(_lint_source_file(index, path, rel))
    return errors


def _unthreaded_calls(
    index: _Index, mod: str, fn_node: ast.AST, clock: frozenset[str]
) -> Iterator[tuple[ast.Call, tuple[str, str]]]:
    """Calls in `fn_node` to real-now fixed-boundary callees that the clock is not threaded into."""
    for sub in ast.walk(fn_node):
        if not isinstance(sub, ast.Call):
            continue
        resolved = index._resolve(mod, sub)
        if resolved is None:
            continue
        cuses, ccovered, cfam = index.summary(*resolved)
        if not (cuses and cfam):
            continue
        if index._inside_fallback(fn_node, sub, clock):
            continue
        if ccovered and index._call_threaded(sub, resolved, clock):
            continue
        yield sub, resolved


def _lint_source_file(index: _Index, path: Path, rel: str) -> list[str]:
    mod = rel[:-3].replace("/", ".")
    errors: list[str] = []
    fns = index.functions(mod)
    for name, (fn_node, params) in fns.items():
        clock = params & _CLOCK_PARAMS
        if not clock:
            continue
        uses, covered, family = index.summary(mod, name)
        if not (uses and not covered and family):
            continue
        seen: set[tuple[str, str]] = set()
        for sub, (cmod, cname) in _unthreaded_calls(index, mod, fn_node, clock):
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


def _lint_tests(index: _Index, paths: list[Path], scope: frozenset[str] | None = None) -> list[str]:
    errors: list[str] = []
    for path in paths:
        if path.is_dir():
            for p in sorted(path.rglob("*.py")):
                rel = _rel_or_abs(p)
                if _is_test_path(rel) and (scope is None or rel in scope):
                    errors.extend(_lint_test_file(index, p, rel))
        elif path.suffix == ".py":
            rel = _rel_or_abs(path)
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

    for sub in ast.walk(function):
        if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)) and sub is not function:
            continue  # nested helper scopes are not tracked here
        if isinstance(sub, ast.Assign):
            for target in sub.targets:
                if isinstance(target, ast.Name):
                    if any(_is_real_now_call(n) for n in ast.walk(sub.value)):
                        real.add(target.id)
                        fixed.discard(target.id)
                    elif _expr_refs_fixed(sub.value, fixed_names, fixed, aliases, index):
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
            and n.attr in index.fixed_names(aliases[n.value.id])
        ):
            return True
    return False


def _opaque_or_real_now(call: ast.Call) -> str | None:
    """The taint label of an opaque HTTP call or a real-clock read; None for anything else."""
    f = call.func
    if isinstance(f, ast.Name) and f.id == "TestClient":
        return "TestClient (opaque HTTP)"
    if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
        if f.value.id in _HTTP_NAMES and f.attr in _HTTP_METHODS:
            return f"{f.value.id}.{f.attr} (opaque HTTP)"
        if _is_real_now_call(call):
            return f"{f.value.id}.{f.attr}"
    return None


def _tainted(function: ast.AST, index: _Index, mod: str) -> list[str]:
    """Real-now contamination sources in one test function body."""
    taints: list[str] = []
    for sub in ast.walk(function):
        if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)) and sub is not function:
            continue
        if not isinstance(sub, ast.Call):
            continue
        opaque = _opaque_or_real_now(sub)
        if opaque is not None:
            taints.append(opaque)
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


# ── rule 3: fixed calendar literals bound to window-shaped names (tests/) ───


def _is_calendar_literal(node: ast.AST) -> bool:
    """A fixed calendar date: an ISO date/datetime string, or a `date(...)` /
    `datetime(...)` construction with at least three constant positional args
    (year, month, day; keywords like `tzinfo=` do not make the date dynamic)."""
    if isinstance(node, ast.Constant):
        return isinstance(node.value, str) and bool(_CALENDAR_STR.match(node.value))
    if isinstance(node, ast.Call):
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        return (
            name in ("date", "datetime")
            and len(node.args) >= 3
            and all(isinstance(arg, ast.Constant) for arg in node.args)
        )
    return False


def _window_bindings(tree: ast.Module) -> Iterator[tuple[str, int, int | None, ast.AST]]:
    """`(name, first line, last line, value)` of every calendar literal bound to a window name."""
    for node in ast.walk(tree):
        yield from _node_bindings(node)


def _node_bindings(node: ast.AST) -> Iterator[tuple[str, int, int | None, ast.AST]]:
    if isinstance(node, ast.Dict):
        yield from _dict_bindings(node)
    elif isinstance(node, ast.keyword):
        if node.arg in _WINDOW_NAMES and _is_calendar_literal(node.value):
            yield node.arg, node.value.lineno, node.value.end_lineno, node.value
    elif (
        isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id in _WINDOW_NAMES
        and _is_calendar_literal(node.value)
    ):
        yield node.targets[0].id, node.targets[0].lineno, node.value.end_lineno, node.value


def _dict_bindings(node: ast.Dict) -> Iterator[tuple[str, int, int | None, ast.AST]]:
    for key, value in zip(node.keys, node.values, strict=True):
        if (
            isinstance(key, ast.Constant)
            and isinstance(key.value, str)
            and key.value in _WINDOW_NAMES
            and _is_calendar_literal(value)
        ):
            yield key.value, key.lineno, value.end_lineno, value


def _lint_fixture_dates(path: Path, tree: ast.Module, lines: list[str]) -> list[str]:
    """A calendar literal bound to a window-shaped name (dict value, keyword
    argument, or plain assignment) must derive from the clock or carry
    `# time-bomb-ok: <reason>` on any line of the binding's span (key..value;
    the value only for a keyword)."""
    found: list[tuple[int, str]] = []

    def check(name: str, start: int, end: int | None, value: ast.AST) -> None:
        stop = end if end is not None else start
        if any(_OPT_OUT in line for line in lines[start - 1 : stop]):
            return
        found.append(
            (
                start,
                f"{path}:{start}: time-bomb fixture date: a fixed calendar "
                f"literal ({ast.unparse(value)}) bound to the window-shaped "
                f"name '{name}'; derive it from the clock or add "
                f"'# {_OPT_OUT}: <reason>' to opt out",
            )
        )

    for name, start, end, value in _window_bindings(tree):
        check(name, start, end, value)

    return [message for _, message in sorted(found)]


def _equality_errors(
    path: Path,
    function: ast.AST,
    source_lines: list[str],
    taints: list[str],
    refs_fixed: Callable[[ast.AST], bool],
) -> list[str]:
    """Exact (in)equalities on fixed-instant-derived values inside a tainted test function."""
    errors: list[str] = []
    for sub in ast.walk(function):
        if not (
            isinstance(sub, ast.Compare)
            and any(isinstance(op, (ast.Eq, ast.NotEq)) for op in sub.ops)
        ):
            continue
        if not any(refs_fixed(side) for side in [sub.left, *sub.comparators]):
            continue
        if any(_OPT_OUT in line for line in source_lines[sub.lineno - 1 : sub.end_lineno]):
            continue
        errors.append(
            f"{path}:{sub.lineno}: time-bomb test: exact equality "
            "on a value derived from a fixed instant while the "
            f"derivation can reach the real clock ({'; '.join(taints)}); "
            "pin the clock (pass now=...), assert with a tolerance, or "
            f"add '# {_OPT_OUT}: <reason>' to opt out"
        )
    return errors


def _lint_test_file(index: _Index, path: Path, rel: str) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text)
    except (OSError, UnicodeDecodeError, SyntaxError):
        return []
    source_lines = text.splitlines()
    errors: list[str] = _lint_fixture_dates(path, tree, source_lines)
    mod = rel[:-3].replace("/", ".")
    fixed_names, aliases = _fixed_names_in_module(index, tree)
    if not fixed_names and not aliases:
        return errors  # nothing fixed-instant in this test module — fast path
    for node in tree.body:
        if not isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef)
        ) or not node.name.startswith("test_"):
            continue
        fixed_local, _real_local = _local_derives(node, fixed_names, aliases, index)
        taints = _tainted(node, index, mod)
        if not taints:
            continue
        errors.extend(
            _equality_errors(
                path,
                node,
                source_lines,
                taints,
                lambda e, fl=fixed_local: _expr_refs_fixed(e, fixed_names, fl, aliases, index),
            )
        )
    return errors


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    argv, only = lint_common.split_only(argv)
    scope = lint_common.changed_scope(only, _REPO_ROOT)
    root = _REPO_ROOT
    if argv:
        paths = [p if p.is_absolute() else root / p for p in (Path(a) for a in argv)]
        missing = [a for a, p in zip(argv, paths, strict=True) if not p.exists()]
        if missing:
            print(f"error: target path(s) not found: {', '.join(missing)}", file=sys.stderr)
            return 1
        used = {p for p in paths if p.is_dir()}
        dirs = _SCAN_DIRS if used else ()
        lint_common.scan_roots(root, dirs)
        index = _Index(root, dirs)
        errors = _lint_source(index, paths) + _lint_tests(index, paths)
    else:
        dirs = _SCAN_DIRS
        index = _Index(root, dirs)
        # Rules 2 and 3 read test files wherever they live: the top-level tests/ and
        # each package's own tests/ (`_lint_tests` keeps only test paths).
        # With `--only` (the commit hook) just the changed files are judged, against the same
        # whole-repo index; what a changed helper does to files that did not change is
        # CI's full run to catch.
        errors = _lint_source(index, lint_common.scan_roots(root, dirs), scope) + _lint_tests(
            index, [root / "tests", *lint_common.scan_roots(root, dirs)], scope
        )
    for err in errors:
        print(err, file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
