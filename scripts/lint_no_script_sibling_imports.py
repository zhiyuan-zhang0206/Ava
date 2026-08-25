"""Require a sys.path guard before sibling imports in script-mode Python files.

Run: `.venv/bin/python scripts/lint_no_script_sibling_imports.py [path ...]`
(defaults to scanning the whole repo). Also run automatically via pre-commit.

## Why

The 2026-08-22 runner hardening sets `PYTHONSAFEPATH=1`: Python no longer
prepends the script's own directory to sys.path. A script-mode file
(`python path/to/script.py`) that imports a same-directory sibling therefore
breaks with ModuleNotFoundError — the 2026-08-23 `daily_scan.py` crash
(`import collect`) was exactly this, and `lint_ava_okf.py` had hit the same
wall the day before.

The fix is a guard that restores the script's directory before the sibling
import:

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # noqa: PTH100, PTH120

(`str(Path(__file__).resolve().parent)` is equivalent.) A guard that derives
the directory from `__file__` survives moves; a hardcoded path does not.

## The rule

A script-mode file (an `if __name__ == "__main__":` block or a python / uv
shebang) may not import a same-directory sibling unless a
`sys.path.insert` / `sys.path.append` call runs first:

- at module top level (including inside top-level `if` / `try` / `with`
  containers, e.g. `if str(_HERE) not in sys.path:`), before the import, or
- earlier in the same function for an in-function import (module-top guards
  always protect in-function imports — module top runs before any call).

## Scope boundaries

- **Module-only files are out of scope** (no main block, no shebang): they are
  reached through guarded importers and never run as scripts. The standing
  examples are `gmail/reference/_smtp.py` and `web-ai/reference/_utils.py` —
  sole importers `feed.py` / `webchat.py` install the guard first. A module
  that grows a main block or shebang enters scope automatically.
- **Tests are excluded**: pytest inserts the test directory into sys.path
  itself, so sibling imports in tests are module mode, not script mode.
- **`from sibling.submodule import X` where `sibling` is a same-dir FILE is
  not flagged**: a file cannot be a package, so the import cannot resolve to
  the sibling — it must come from elsewhere on sys.path (e.g. a top-level
  package of the same name). A sibling PACKAGE directory is a hazard for any
  dotted path and is flagged.
- Imports inside `if TYPE_CHECKING:` are not flagged (never executed).
"""

import ast
import re
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent

# Directories that are never scanned (vendored / generated / scratch).
_EXCLUDED_DIRS = frozenset(
    {
        ".git",
        ".venv",
        "node_modules",
        "__pycache__",
        ".next",
        "dist",
        "build",
        "outputs",
        "runs",
        "logs",
        "tmp",
        ".worktrees",
        ".claude",
        "site-packages",
    }
)

# Test files are pytest-managed (pytest inserts their directory into
# sys.path), never script-mode.
_TEST_PATTERNS = (
    re.compile(r"(^|/)tests?/"),
    re.compile(r"(^|/)test_[^/]+\.py$"),
    re.compile(r"_test\.py$"),
)

# A python shebang (`#!/usr/bin/env python3`) or a PEP 723 uv script
# (`#!/usr/bin/env -S uv run --script`) runs the file as a script.
_SHEBANG_RE = re.compile(r"^#!.*\b(?:python\d*(?:\.\d+)?|uv run)\b")


def _is_script_mode(tree: ast.Module, src: str) -> bool:
    """True when the file runs as a script (main block or python/uv shebang)."""
    first_line = src.splitlines()[0] if src else ""
    if _SHEBANG_RE.search(first_line):
        return True
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.If)
            and isinstance(node.test, ast.Compare)
            and isinstance(node.test.left, ast.Name)
            and node.test.left.id == "__name__"
        ):
            for op, cmp in zip(node.test.ops, node.test.comparators, strict=False):
                if (
                    isinstance(op, ast.Eq)
                    and isinstance(cmp, ast.Constant)
                    and cmp.value == "__main__"
                ):
                    return True
    return False


def _is_sys_path_guard(stmt: ast.stmt) -> bool:
    """True when stmt is a `sys.path.insert(...)` / `sys.path.append(...)` call."""
    if not isinstance(stmt, ast.Expr):
        return False
    call = stmt.value
    if not isinstance(call, ast.Call):
        return False
    fn = call.func
    return (
        isinstance(fn, ast.Attribute)
        and fn.attr in ("insert", "append")
        and isinstance(fn.value, ast.Attribute)
        and fn.value.attr == "path"
        and isinstance(fn.value.value, ast.Name)
        and fn.value.value.id == "sys"
    )


def _type_checking_ids(tree: ast.Module) -> set[int]:
    """ids of every node inside an `if TYPE_CHECKING:` block (never executed)."""
    ids: set[int] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.If)
            and isinstance(node.test, ast.Name)
            and node.test.id == "TYPE_CHECKING"
        ):
            for child in ast.walk(node):
                ids.add(id(child))
    return ids


def _sibling_targets(stmt: ast.Import | ast.ImportFrom, d: Path) -> list[tuple[str, str]]:
    """Same-dir modules the statement could resolve to: (first_component, kind).

    kind = "file" | "pkg". `from sibling.sub import X` with a sibling FILE is
    excluded — a file cannot be a package, so the import resolves elsewhere.
    """
    out: list[tuple[str, str]] = []
    if isinstance(stmt, ast.Import):
        for name in stmt.names:
            first = name.name.split(".")[0]
            if (d / f"{first}.py").is_file():
                if "." not in name.name:  # bare `import sibling` binds the sibling file
                    out.append((first, "file"))
            elif (d / first / "__init__.py").is_file():
                out.append((first, "pkg"))
    elif stmt.module is not None:
        first = stmt.module.split(".")[0]
        if (d / f"{first}.py").is_file():
            if "." not in stmt.module:  # `from sibling import X`
                out.append((first, "file"))
        elif (d / first / "__init__.py").is_file():
            out.append((first, "pkg"))
    return out


def _scan_file(path: Path) -> list[str]:
    """Return one error string per unguarded sibling import in a script-mode file."""
    try:
        src = path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(src)
    except (OSError, SyntaxError):
        return []
    if not _is_script_mode(tree, src):
        return []
    try:
        rel = path.relative_to(_REPO_ROOT).as_posix()
    except ValueError:
        # Path not under repo — pre-commit usually passes absolute paths so this
        # is rare; safety net so an out-of-tree target cannot crash the run.
        rel = path.as_posix()
    d = path.parent
    type_checking = _type_checking_ids(tree)
    violations: list[str] = []
    top_guards: list[int] = []

    def walk(stmts: list[ast.stmt], fn_guards: list[int], *, in_fn: bool) -> None:
        for stmt in stmts:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                walk(stmt.body, [], in_fn=True)
            elif isinstance(stmt, ast.ClassDef):
                walk(stmt.body, fn_guards, in_fn=in_fn)
            elif _is_sys_path_guard(stmt):
                (fn_guards if in_fn else top_guards).append(stmt.lineno)
            elif isinstance(stmt, ast.If):
                walk(stmt.body, fn_guards, in_fn=in_fn)
                walk(stmt.orelse, fn_guards, in_fn=in_fn)
            elif isinstance(stmt, ast.Try):
                walk(stmt.body, fn_guards, in_fn=in_fn)
                for handler in stmt.handlers:
                    walk(handler.body, fn_guards, in_fn=in_fn)
                walk(stmt.orelse, fn_guards, in_fn=in_fn)
                walk(stmt.finalbody, fn_guards, in_fn=in_fn)
            elif isinstance(stmt, (ast.For, ast.While)):
                walk(stmt.body, fn_guards, in_fn=in_fn)
                walk(stmt.orelse, fn_guards, in_fn=in_fn)
            elif isinstance(stmt, (ast.With, ast.AsyncWith)):
                walk(stmt.body, fn_guards, in_fn=in_fn)
            elif isinstance(stmt, (ast.Import, ast.ImportFrom)) and not (
                isinstance(stmt, ast.ImportFrom) and stmt.level > 0
            ):
                if id(stmt) in type_checking:
                    continue
                for first, _kind in _sibling_targets(stmt, d):
                    if in_fn:
                        guarded = bool(top_guards) or any(g < stmt.lineno for g in fn_guards)
                    else:
                        guarded = any(g < stmt.lineno for g in top_guards)
                    if not guarded:
                        violations.append(
                            f"{rel}:{stmt.lineno}: sibling import `{first}` in a script-mode "
                            "file — PYTHONSAFEPATH=1 keeps the script's own directory off "
                            "sys.path. Add a guard before it: "
                            "sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  "
                            "# noqa: PTH100, PTH120"
                        )

    walk(tree.body, [], in_fn=False)
    return violations


def _iter_py_files(roots: list[Path]) -> list[Path]:
    files: list[Path] = []
    for root in roots:
        if root.is_file() and root.suffix == ".py":
            files.append(root)
        elif root.is_dir():
            for p in root.rglob("*.py"):
                if any(part in _EXCLUDED_DIRS for part in p.parts):
                    continue
                files.append(p)
    return files


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    targets = [Path(a).resolve() for a in argv] if argv else [_REPO_ROOT]
    total = 0
    for path in sorted(_iter_py_files(targets)):
        rel = (
            path.relative_to(_REPO_ROOT).as_posix()
            if path.is_relative_to(_REPO_ROOT)
            else path.as_posix()
        )
        if any(p.search(rel) for p in _TEST_PATTERNS):
            continue
        for violation in _scan_file(path):
            total += 1
            print(violation)
    if total:
        print(
            f"\n{total} violation(s). See the header of scripts/lint_no_script_sibling_imports.py "
            "for the guard pattern.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
