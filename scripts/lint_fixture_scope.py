"""Forbid a pytest fixture whose scope outlives the blast radius of the process
global it mutates.

Run: `.venv/bin/python scripts/lint_fixture_scope.py [path ...]` (defaults to
scanning `tests/`). Also run automatically via pre-commit hook.

## Why

A session-scoped fixture's teardown fires at the end of the pytest *session*, not
when pytest leaves the directory the fixture was written for. So a session-scoped
fixture that reassigns a process global and puts it back in a `finally` does not
restore anything on behalf of the tests that follow it — every test collected after
its directory, in the same process, runs with the mutated value still installed. The
`finally` reads as a cleanup and is one.

That is not hypothetical. `tests/e2e/conftest.py:_e2e_process_env` reassigns
`AVA_HOME`, `AVA_GATEWAY_URL` and eight more, restores them in a `finally`, and was
declared `scope="session"`. `tests/test_home_isolation.py` — the file whose whole job
is to notice the operator's real home leaking into the test process — sorts after
`tests/e2e/` and failed two of its four assertions on every serial run, on `main`,
for as long as the keyword said `session`. It stayed invisible because CI's backend
job passes `--ignore=tests/e2e` (the two files never share a process there) and
`-n auto` puts them in different workers regardless, so the only reader who ever saw
the red had every reason to write it off as someone else's flake.

Nothing caught it, and nothing would have caught the next one. Hence this lint.

## Rule 1 — session scope outside the root conftest may not mutate a process global

`tests/conftest.py` is exempt, and it is the only exemption: it is the root conftest,
so "the session" and "my directory" are the same blast radius. Its
`_provisioned_db` / `_provisioned_redis` assign `AVA_DB_URL` / `AVA_REDIS_URL` and
never put them back, which is correct — they establish the session's baseline rather
than claim to clean up after themselves.

Anywhere deeper, session scope plus a process-global mutation is a contradiction by
construction: the fixture exists to serve one subtree and its teardown cannot fire
when pytest leaves that subtree. Narrow the scope (`package` — see Rule 2 — or
`module` / `function`), or hoist the value to the root conftest if it really is
session-wide.

A session-scoped fixture that mutates nothing process-global is fine and is not
reported: `tests/e2e/conftest.py`'s `frontend_proc` / `playwright_runtime` /
`playwright_browser` own an expensive resource (a Next build, a browser) whose
lifetime genuinely is the session, and they hand it back through the fixture return
value rather than through a global. That is the shape this lint is careful not to
tax, because a lint that has to be silenced on every legitimate case is worse than
no lint.

## Rule 2 — `scope="package"` requires an `__init__.py`

`_pytest.fixtures.get_scope_package` walks the node's parents for a `Package` whose
nodeid matches the fixture's and **returns `node.session` when it finds none** — no
warning, no error. pytest builds a `Package` node only for a directory containing
`__init__.py`. So in a package-less directory `scope="package"` is an exact synonym
for `scope="session"`: the keyword reads correctly and does nothing.

That is what made the first draft of the fix above a no-op, and it is invisible in
review — the diff shows `session` -> `package` and looks complete. `tests/e2e/` is
the only test directory in this repo with an `__init__.py`, and it has one for
exactly this reason. Every other `scope="package"` in `tests/` is a lie until
someone adds the file.

## Polarity

The default answer is "this is a finding". A `scope=` argument this script cannot
read as a string literal is reported rather than assumed harmless, and the mutation
check allow-lists the `os.environ` methods known to be read-only rather than
deny-listing the ones known to mutate — so a dict mutator nobody thought of (or an
`os.environ |= {...}`) is caught by default instead of waved through.

## What it does NOT see

A mutation the fixture makes by CALLING something else — the check is per-function,
not interprocedural, so a fixture whose body is `_layer_env()` reads as clean. Worth
knowing, but the shape it misses is the one a reviewer can see (a fixture that calls
a helper named after what it changes), unlike the two rules above.

Mutation of some other module's mutable container through a method call —
`sys.path.append(...)`, `logging.root.setLevel(...)`. Those are not syntactic
mutations and separating them from the ordinary `shutil.rmtree(...)` /
`subprocess.run(...)` calls a fixture body is full of would take a deny-list of
method names, which is the polarity this file argues against. The surface covered is
the one this repo has declared its runtime-config surface — env vars and
`shared.config.settings` fields (see `scripts/lint_no_os_environ.py`) — plus every
`Store` / `Del` target and `global` declaration, which is exhaustive over the
syntactic forms.

Error format `file:line: <reason>` + non-zero exit.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent

# The one location where session scope and the fixture's own blast radius coincide.
_ROOT_CONFTEST = "tests/conftest.py"

# Fixture-level exemption, keyed `<relpath>::<fixture_name>`. Deliberately EMPTY:
# a session-scoped fixture outside the root conftest that mutates a process global
# has no correct form, so the fix is always to narrow the scope or hoist the value —
# not to record the exception here. Kept as a named seam so that a future case with
# a real argument has somewhere to state it, next to the reason.
_ALLOWED_SESSION_MUTATORS: frozenset[str] = frozenset()

# Decorator paths that make a function a fixture.
_FIXTURE_DECORATORS = frozenset({"pytest.fixture", "pytest_asyncio.fixture", "fixture"})

# `os.environ` methods that only read. Allow-list, not deny-list: any other
# attribute called on os.environ counts as a mutation, so `|=`, a `pop` alias, or
# whatever dict API arrives next is a finding without this file being updated.
_ENVIRON_READ_ONLY = frozenset(
    {
        "get",
        "keys",
        "values",
        "items",
        "copy",
        "__contains__",
        "__getitem__",
        "__iter__",
        "__len__",
    }
)

# Modules a local can be bound to as a live VIEW rather than a copy, so writing
# through the local reaches the global: `env = os.environ`, `dp =
# settings.data_plane`. Only these two need tracking — they are the repo's declared
# runtime-config surface (`shared.config.settings` plus the environment). A write
# whose root is any other non-local name is already a finding on the general rule
# below; this list exists only so the alias bypass is closed too.
_ALIASABLE_GLOBAL_ROOTS = frozenset({"os", "settings"})

# Scopes whose teardown fires later than the directory the fixture is written for.
_SESSION = "session"
_PACKAGE = "package"


def _dotted(node: ast.expr) -> str | None:
    """`os.environ` / `settings.data_plane.db_url` -> the dotted string; None if the
    expression is not a plain attribute chain rooted at a bare name."""
    parts: list[str] = []
    cur: ast.expr = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if not isinstance(cur, ast.Name):
        return None
    parts.append(cur.id)
    return ".".join(reversed(parts))


def _root_name(node: ast.expr) -> str | None:
    """The bare name an assignment target is rooted at: `os.environ["X"]` -> "os",
    `cfg.a.b` -> "cfg", `local` -> "local". None for anything not rooted at a Name
    (a call result, a literal, a starred unpack of a call)."""
    cur: ast.expr = node
    while True:
        if isinstance(cur, ast.Name):
            return cur.id
        if isinstance(cur, ast.Attribute | ast.Subscript):
            cur = cur.value
            continue
        if isinstance(cur, ast.Starred):
            cur = cur.value
            continue
        return None


def _bound_names(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> tuple[set[str], set[str]]:
    """Return (locals, aliases_of_globals) for a fixture body.

    `locals` are names the function itself binds — parameters, assignment targets,
    `for`/`with`/`except`/comprehension targets, walrus, and imports made inside the
    body. Writing through one of those is a write to function-local state.

    `aliases_of_globals` are locals bound *directly* to a global attribute chain
    (`env = os.environ`, `dp = settings.data_plane`). Writing through one of those
    reaches the global, so they are excluded from `locals`. Only no-call chains are
    treated as aliases: `conn = psycopg.connect(...)` binds a fresh object, not a
    view onto a module.
    """
    args = fn.args
    params = [*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg]
    local: set[str] = {a.arg for a in params if a is not None}
    aliases: set[str] = set()
    declared_global: set[str] = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Global | ast.Nonlocal):
            declared_global.update(node.names)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            local.add(node.id)
        elif isinstance(node, ast.Import | ast.ImportFrom):
            for entry in node.names:
                local.add((entry.asname or entry.name).split(".")[0])
        elif isinstance(node, ast.Assign) and _root_name(node.value) in _ALIASABLE_GLOBAL_ROOTS:
            # `env = os.environ` aliases the global; `conn = psycopg.connect(...)`
            # does not, so only a call-free attribute chain counts.
            if _dotted(node.value) is None:
                continue
            aliases.update(t.id for t in node.targets if isinstance(t, ast.Name))
    return local - declared_global - aliases, aliases


def _global_writes(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> list[tuple[int, str]]:
    """Every process-global write in a fixture body, as (lineno, description).

    Covers the syntactic mutation forms exhaustively — any `Store`/`Del` target
    whose root is not function-local, plus a `global` declaration — and adds the one
    non-syntactic form that matters for runtime config: a mutating method call on
    `os.environ`.
    """
    local, aliases = _bound_names(fn)
    writes: list[tuple[int, str]] = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Global):
            writes.append((node.lineno, f"global {', '.join(node.names)}"))
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            owner = _dotted(node.func.value)
            if owner in {"os.environ", *aliases} and node.func.attr not in _ENVIRON_READ_ONLY:
                writes.append((node.lineno, f"{owner}.{node.func.attr}(...)"))
        elif isinstance(node, ast.Attribute | ast.Subscript) and isinstance(
            node.ctx, ast.Store | ast.Del
        ):
            # Only the OUTERMOST node of a target carries Store/Del ctx (in
            # `os.environ["X"] = v` the inner `os.environ` is a Load), so each write
            # is seen once. A root that is not function-local reaches out of the
            # fixture — that is the whole test, and it needs no list of known
            # globals to state.
            root = _root_name(node)
            if root is not None and root not in local:
                writes.append((node.lineno, _describe(node)))
    return writes


def _describe(node: ast.expr) -> str:
    """Render an assignment target for the error message."""
    if isinstance(node, ast.Subscript):
        base = _dotted(node.value) or "?"
        key = node.slice
        if isinstance(key, ast.Constant):
            return f"{base}[{key.value!r}]"
        return f"{base}[...]"
    return _dotted(node) or "?"


def _fixture_scope(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> tuple[str | None, bool]:
    """Return (scope, is_fixture). scope is None when the decorator IS a fixture but
    its `scope=` argument is not a readable string literal — reported rather than
    assumed to be `function`."""
    for dec in fn.decorator_list:
        call = dec if isinstance(dec, ast.Call) else None
        base = call.func if call else dec
        if _dotted(base) not in _FIXTURE_DECORATORS:
            continue
        if call is None:
            return "function", True
        for kw in call.keywords:
            if kw.arg != "scope":
                continue
            if isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                return kw.value.value, True
            return None, True
        return "function", True
    return None, False


def findings_in_source(src: str, rel_path: str, *, has_package_init: bool) -> list[tuple[int, str]]:
    """Every finding in one test module's source, as (lineno, message).

    `has_package_init` is whether the file's own directory contains an
    `__init__.py` — the thing that decides whether `scope="package"` means package
    or silently means session.
    """
    tree = ast.parse(src)
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        scope, is_fixture = _fixture_scope(node)
        if not is_fixture:
            continue
        if scope is None:
            out.append(
                (
                    node.lineno,
                    f"fixture `{node.name}` passes a non-literal `scope=`. This lint cannot "
                    "tell whether its teardown outlives its blast radius, so it is reported "
                    "rather than assumed safe — pass a string literal.",
                )
            )
            continue
        effective = scope
        if scope == _PACKAGE and not has_package_init:
            effective = _SESSION
            out.append(
                (
                    node.lineno,
                    f'fixture `{node.name}` is `scope="package"` but '
                    f"{Path(rel_path).parent.as_posix()}/__init__.py does not exist, so pytest "
                    "collects the directory as a plain Dir. `get_scope_package` finds no "
                    "Package node and falls back to the SESSION node — the keyword reads "
                    "'package' and means 'session', with no warning. Add the __init__.py "
                    "(and keep it: deleting it re-opens this silently), or use "
                    '`scope="module"`.',
                )
            )
        if effective != _SESSION or rel_path == _ROOT_CONFTEST:
            continue
        if f"{rel_path}::{node.name}" in _ALLOWED_SESSION_MUTATORS:
            continue
        writes = _global_writes(node)
        if not writes:
            continue
        listed = ", ".join(sorted({desc for _, desc in writes}))
        out.append(
            (
                node.lineno,
                f"fixture `{node.name}` is session-scoped outside {_ROOT_CONFTEST} and "
                f"mutates process globals ({listed}). Its teardown fires at end-of-SESSION, "
                f"not when pytest leaves {Path(rel_path).parent.as_posix()}/ — so every test "
                "collected after that directory in the same process keeps running with these "
                "values installed. Narrow the scope to `package` (the directory needs an "
                "__init__.py or the keyword silently means session) / `module` / `function`, "
                f"or hoist the value to {_ROOT_CONFTEST}, where the session IS the blast "
                "radius.",
            )
        )
    return out


def setup_env_keys(src: str, fixture_name: str) -> tuple[frozenset[str], frozenset[str]]:
    """The `os.environ` keys one named fixture writes during SETUP, as
    (literal_keys, dynamic_exprs).

    Setup only — everything before the `yield`. A generator fixture's writes after
    the yield are its teardown, and for the fixture this exists to guard those writes
    ARE the restore (`os.environ[k] = v` over the saved mapping). Counting them as
    keys-that-need-restoring would report the restore loop's loop variable as an
    unresolvable key and make the guard vacuous.

    Used by `tests/test_home_isolation.py` to check that
    `tests/e2e/conftest.py:_e2e_process_env`'s save/restore tuple covers every key
    its body actually assigns, so the tuple cannot drift behind the body. A
    non-literal key lands in `dynamic_exprs` so the caller fails loudly rather than
    silently under-reporting.
    """
    tree = ast.parse(src)
    literal: set[str] = set()
    dynamic: set[str] = set()

    def _record(key: ast.expr) -> None:
        if isinstance(key, ast.Constant) and isinstance(key.value, str):
            literal.add(key.value)
        else:
            dynamic.add(ast.unparse(key))

    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if node.name != fixture_name:
            continue
        yields = [n.lineno for n in ast.walk(node) if isinstance(n, ast.Yield)]
        teardown_starts = min(yields) if yields else None
        for inner in ast.walk(node):
            lineno = getattr(inner, "lineno", 0)
            if teardown_starts is not None and lineno > teardown_starts:
                continue
            if (
                isinstance(inner, ast.Subscript)
                and _dotted(inner.value) == "os.environ"
                and isinstance(inner.ctx, ast.Store | ast.Del)
            ):
                _record(inner.slice)
            elif (
                isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Attribute)
                and _dotted(inner.func.value) == "os.environ"
                and inner.func.attr not in _ENVIRON_READ_ONLY
                and inner.args
            ):
                _record(inner.args[0])
    return frozenset(literal), frozenset(dynamic)


def _scan_file(path: Path, rel_path: str) -> list[tuple[int, str]]:
    has_init = (path.parent / "__init__.py").is_file()
    return findings_in_source(path.read_text(encoding="utf-8"), rel_path, has_package_init=has_init)


def _iter_py_files(roots: list[Path]) -> list[Path]:
    files: list[Path] = []
    for root in roots:
        if root.is_file() and root.suffix == ".py":
            files.append(root)
        elif root.is_dir():
            files.extend(root.rglob("*.py"))
    return [f for f in files if "__pycache__" not in f.parts]


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    targets = [Path(a).resolve() for a in argv] if argv else [_REPO_ROOT / "tests"]

    total = 0
    for path in sorted(_iter_py_files(targets)):
        try:
            rel = path.relative_to(_REPO_ROOT).as_posix()
        except ValueError:
            rel = path.as_posix()
        if not rel.startswith("tests/"):
            continue
        for lineno, message in _scan_file(path, rel):
            total += 1
            print(f"{rel}:{lineno}: error: {message}")

    if total:
        print(
            f"\n{total} fixture-scope violations. See the docstring at the top of "
            "scripts/lint_fixture_scope.py for the two rules and why session scope is "
            "exempt only in the root conftest.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
