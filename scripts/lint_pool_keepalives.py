"""Forbid building a psycopg connection pool without TCP keepalives.

Run: `.venv/bin/python scripts/lint_pool_keepalives.py [path ...]` (defaults to
scanning the whole repo). Also run automatically via pre-commit hook.

## Why

Pool connections are **long-lived**, which is what makes a missing keepalive
invisible until it costs minutes. `shared/db.py:PG_KEEPALIVE_KWARGS` documents the
mechanism: a laptop-grade runner that sleeps or changes networks wakes holding
dead TCP flows, and a query already in flight on a borrowed half-dead socket has
no application-level bound — it waits out the OS TCP-retransmit timeout.

`shared.db.pool()` merges those kwargs for every sync pool, so the fix for a call
site is normally "call `shared.db.pool()`". Two pools legitimately cannot: the
agent's `LoggingConnectionPool` and the eval driver's pools are **async**
(`AsyncConnectionPool`), a different class with a different lifecycle, and
`shared.db` has no async pool factory. Those spell out
`{..., **PG_KEEPALIVE_KWARGS}` themselves, which is what this lint accepts —
`PG_KEEPALIVE_KWARGS` stays the single definition of the *values* even where it
cannot be the single definition of the *call*.

Without this check the invariant is "three call sites each remembered", which is
exactly the state that produced the defect: the sync pools in `shared/log.py`,
`gateway/app.py` and `services/agent_ops/daemon.py` all wrote
`kwargs={"prepare_threshold": None}` and stopped there, and PR #940's sweep of the
bare `psycopg.connect` sites left them untouched because they are a different
failure mode (a pool borrow is already capped by the pool's acquire timeout, so
they never produced the boot-hang that PR fixed).

## The rule

Every call to a class whose name ends in `ConnectionPool` must pass a `kwargs=`
dict containing `**PG_KEEPALIVE_KWARGS`. AST-based, so it sees through
`AsyncConnectionPool[psycopg.AsyncConnection](...)` subscripts and subclasses
(`LoggingConnectionPool`) without regex guesswork.

Exempt: `shared/db.py` (the definition of the posture — it builds the merged dict
literal that every other site inherits) and test/eval-fixture code under
`tests/`, where a throwaway pool against a local test Postgres has nothing to
survive. `ConnectionPool.check_connection(...)` and bare type annotations are not
constructions and are not flagged.

Error format `file:line: <message>` + non-zero exit.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent

# Scan directories — only OUR code, never .venv / node_modules / vendored trees.
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

# `shared/db.py` owns the merged kwargs dict every other site routes through, so
# it is the one file that writes the values as a literal rather than unpacking
# the constant. Any addition here must show it cannot reach the constant at all.
_ALLOWED_FILES = frozenset({"shared/db.py"})

_TEST_PATTERNS = (
    re.compile(r"(^|/)tests?/"),
    re.compile(r"(^|/)test_[^/]+\.py$"),
    re.compile(r"_test\.py$"),
)

_KEEPALIVE_NAME = "PG_KEEPALIVE_KWARGS"


def _is_test_file(rel_path: str) -> bool:
    return any(p.search(rel_path) for p in _TEST_PATTERNS)


def _called_class_name(func: ast.expr) -> str | None:
    """Name of the class being constructed, seeing through generic subscripts.

    `ConnectionPool(...)` -> "ConnectionPool";
    `AsyncConnectionPool[psycopg.AsyncConnection](...)` -> "AsyncConnectionPool";
    `psycopg_pool.ConnectionPool(...)` -> "ConnectionPool".
    Returns None for anything that is not a plain name/attribute/subscript call,
    which includes `ConnectionPool.check_connection(...)` — that resolves to
    "check_connection" and simply does not match the suffix test below.
    """
    if isinstance(func, ast.Subscript):
        return _called_class_name(func.value)
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _unpacks_keepalives(kwargs_value: ast.expr) -> bool:
    """True when the `kwargs=` value is a dict literal containing
    `**PG_KEEPALIVE_KWARGS`.

    A `**`-unpack in a dict literal is an entry whose key is None, so the check is
    "some entry has no key and its value names the constant". Deliberately does
    not accept a non-literal (`kwargs=some_dict`): this lint reads source, and a
    name it cannot see through would be an unverifiable pass.
    """
    if not isinstance(kwargs_value, ast.Dict):
        return False
    for key, value in zip(kwargs_value.keys, kwargs_value.values, strict=True):
        if key is not None:
            continue
        if isinstance(value, ast.Name) and value.id == _KEEPALIVE_NAME:
            return True
        if isinstance(value, ast.Attribute) and value.attr == _KEEPALIVE_NAME:
            return True
    return False


def violations_in_source(src: str, filename: str = "<source>") -> list[tuple[int, str]]:
    """Return [(lineno, message), ...] for pool constructions missing keepalives.

    Takes source rather than a path so the lint's own tests can drive it with
    literal snippets (same shape as scripts/lint_termination_source.py).
    """
    try:
        tree = ast.parse(src, filename=filename)
    except SyntaxError as exc:
        return [(exc.lineno or 1, f"could not parse: {exc}")]

    violations: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        class_name = _called_class_name(node.func)
        if class_name is None or not class_name.endswith("ConnectionPool"):
            continue
        kwargs_arg = next((kw for kw in node.keywords if kw.arg == "kwargs"), None)
        if kwargs_arg is None:
            violations.append(
                (
                    node.lineno,
                    f"{class_name}(...) passes no `kwargs=` — a pool built without "
                    f"`**{_KEEPALIVE_NAME}` has no TCP keepalives, so a borrowed "
                    "connection on a half-dead socket hangs on the OS "
                    "TCP-retransmit timeout (minutes, unbounded)",
                )
            )
        elif not _unpacks_keepalives(kwargs_arg.value):
            violations.append(
                (
                    node.lineno,
                    f"{class_name}(kwargs=...) does not unpack `**{_KEEPALIVE_NAME}` "
                    "— for a sync pool call `shared.db.pool()` instead of "
                    "constructing one; for an async pool add "
                    f"`**{_KEEPALIVE_NAME}` to the kwargs dict",
                )
            )
    return violations


def _scan_file(path: Path, rel_path: str) -> list[tuple[int, str]]:
    """`violations_in_source` for one file, minus the exempt paths."""
    if rel_path in _ALLOWED_FILES or _is_test_file(rel_path):
        return []
    return violations_in_source(path.read_text(encoding="utf-8"), str(path))


def _iter_py_files(roots: list[Path]) -> list[Path]:
    files: list[Path] = []
    for root in roots:
        if root.is_file() and root.suffix == ".py":
            files.append(root)
        elif root.is_dir():
            files.extend(root.rglob("*.py"))
    return files


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    # argv non-empty = pre-commit passed the changed-file list; empty = full scan.
    targets = [Path(a).resolve() for a in argv] if argv else [_REPO_ROOT / d for d in _SCAN_DIRS]

    total = 0
    for path in sorted(_iter_py_files(targets)):
        try:
            rel = path.relative_to(_REPO_ROOT).as_posix()
        except ValueError:
            rel = path.as_posix()
        for lineno, message in _scan_file(path, rel):
            total += 1
            print(f"{rel}:{lineno}: {message}")

    if total:
        print(
            f"\n{total} pool(s) built without TCP keepalives. See the docstring at the "
            "top of scripts/lint_pool_keepalives.py and shared/db.py:pool().",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
