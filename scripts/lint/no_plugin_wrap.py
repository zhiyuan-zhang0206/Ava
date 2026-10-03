"""Forbid bare monkey-patching of `ava.*` in plugins — wraps must be declared as `SdkWrap`.

Run: `.venv/bin/python scripts/lint/no_plugin_wrap.py [path ...]` (defaults to scanning
`ava_builtins/plugins/`, the built-in plugins; a missing default dir is an error;
an explicit path that does not exist is an error (stderr + exit 1) rather than a
silent no-op). Also run automatically via pre-commit hook before commit.

## Why

A plugin's `contribute()` declares `SdkWrap(target, wrapper)` to extend
SDK behavior; the SDK install applies it: it makes the wrap stack enumerable (`ava.extend.stack`),
deterministic (registration = plugin load order), and reversible (originals
restored on reload). A plugin that instead does `ava.files.read = my_read`
(or `setattr(ava.files, "read", my_read)`) monkey-patches invisibly — no record
of who changed it or in what order, and a reload double-wraps. That bypass is
what this lint bans, so the introspectable stack stays the single source of
truth for "what did plugins inject."

## Rule

In every `plugin.py` (and sibling modules) under `ava_builtins/plugins/`, an assignment
whose target is an `ava`-rooted attribute path ending in a function-style name
is an error:

    ava.files.read = _wrapped_read          # error -> SdkWrap("files.read", ...)
    ava.agents.spawn = _spawn_with_label    # error -> SdkWrap("agents.spawn", ...)
    setattr(ava.files, "read", _wrapped)    # error -> SdkWrap(...)

Not flagged: dunder assignments (`ava.files.__doc__ = ...` — a legitimate
module-docstring override) and ALL-CAPS constant assignments
(`ava.memory.PATH = ...`). New namespaces / members are declared as
`SdkNamespace` / `SdkMember` (Call nodes, never matched here).

Known gap: assignment through a module alias (`import ava.files as f; f.read = x`)
is not caught — the target must be a literal `ava.…` attribute chain. The
primitive is the obvious path; this catches the naive regression.

## Exemption

Append `# wrap-ok: <reason>` on the offending line for a deliberate, reviewed
bare assignment (rare). A test file (any `tests/` directory under the plugins,
e.g. `ava_builtins/plugins/<plugin>/tests/`) is exempt: a test stubs the SDK on
purpose. Error format `file:line: <line>` + non-zero exit.
"""

from __future__ import annotations

import ast
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TypeGuard

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from scripts.structure import lint_common  # noqa: E402 - standalone script

_SCAN_DIR = "ava_builtins/plugins"

_EXEMPTION = "# wrap-ok:"


def _root_name(node: ast.expr) -> str | None:
    """Leftmost `Name.id` of an attribute chain (`ava.files.read` -> "ava"), else None."""
    while isinstance(node, ast.Attribute):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def _is_function_style(attr: str) -> bool:
    """A plain lowercase identifier — an SDK callable name (read / spawn / run).

    Excludes dunders (`__doc__`) and ALL-CAPS constants (`PATH`), which are
    legitimate attribute assignments, not wraps.
    """
    if attr.startswith("_"):
        return False
    return attr != attr.upper()  # ALL-CAPS constants are not function-style


def _attr_path(node: ast.Attribute) -> str:
    """Render `ava.files.read` from the AST target for the error message."""
    parts: list[str] = []
    cur: ast.expr = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
    return ".".join(reversed(parts))


def _assign_violations(
    node: ast.Assign | ast.AnnAssign, exempt: Callable[[int], bool]
) -> list[tuple[int, str]]:
    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    out: list[tuple[int, str]] = []
    for tgt in targets:
        if (
            isinstance(tgt, ast.Attribute)
            and _root_name(tgt) == "ava"
            and _is_function_style(tgt.attr)
            and not exempt(node.lineno)
        ):
            path_str = _attr_path(tgt)
            out.append(
                (
                    node.lineno,
                    f"bare `{path_str} = ...` monkey-patches the SDK — use "
                    f'`SdkWrap("{path_str.removeprefix("ava.")}", wrapper)`',
                )
            )
    return out


def _is_ava_setattr(node: ast.AST) -> TypeGuard[ast.Call]:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "setattr"
        and bool(node.args)
        and _root_name(node.args[0]) == "ava"
    )


def _scan_file(path: Path) -> list[tuple[int, str]]:
    """Return [(lineno, message), ...] for bare ava monkey-patches."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return []  # unreadable entry (e.g. a dangling symlink) or binary content
    try:
        tree = ast.parse("\n".join(lines), filename=str(path))
    except SyntaxError:
        return []  # a broken plugin fails elsewhere; not this lint's job

    def exempt(lineno: int) -> bool:
        return 1 <= lineno <= len(lines) and _EXEMPTION in lines[lineno - 1]

    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        # `ava.x.y = ...` / `ava.x.y: T = ...`
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            out.extend(_assign_violations(node, exempt))
        # `setattr(ava.<x>, "name", ...)`
        elif _is_ava_setattr(node) and not exempt(node.lineno):
            out.append(
                (
                    node.lineno,
                    "bare `setattr(ava..., ...)` monkey-patches the SDK — use "
                    "`SdkWrap(target, wrapper)` in `contribute()` (or SdkNamespace / SdkMember for new members)",
                )
            )
    return out


def _iter_plugin_files(targets: list[Path]) -> list[Path]:
    files: list[Path] = []
    for root in targets:
        if root.is_file() and root.suffix == ".py":
            files.append(root)
        elif root.is_dir():
            files.extend(root.rglob("*.py"))
    return files


def _under_plugins(path: Path) -> bool:
    try:
        rel = path.resolve().relative_to(_REPO_ROOT).as_posix()
    except ValueError:
        return False
    return rel == _SCAN_DIR or rel.startswith(f"{_SCAN_DIR}/")


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    argv, only = lint_common.split_only(argv)
    # argv non-empty = explicit paths (any dir); keep only plugin paths (the
    # plugins dir itself included, so it enumerates its members).
    # Empty = default full scan of the plugins dir, or the `--only` changed files
    # (the commit hook) under that same scope.
    if argv:
        missing = [arg for arg in argv if not Path(arg).exists()]
        if missing:
            print(f"error: target path(s) not found: {', '.join(missing)}", file=sys.stderr)
            return 1
        targets = [p for p in (Path(a).resolve() for a in argv) if _under_plugins(p)]
    else:
        targets = lint_common.scan_roots(_REPO_ROOT, (_SCAN_DIR,))

    scope = lint_common.changed_scope(only, _REPO_ROOT)

    total = 0
    for path in sorted(lint_common.restrict(_iter_plugin_files(targets), scope, _REPO_ROOT)):
        try:
            rel = path.relative_to(_REPO_ROOT).as_posix()
        except ValueError:
            rel = path.as_posix()
        if lint_common.is_repo_test_file(path, _REPO_ROOT):
            continue
        for lineno, message in _scan_file(path):
            total += 1
            print(f"{rel}:{lineno}: {message}")

    if total:
        print(
            f"\n{total} bare-wrap violation(s). Route SDK wraps through "
            "`SdkWrap`; see scripts/lint/no_plugin_wrap.py for the "
            "`# wrap-ok:` exemption.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
