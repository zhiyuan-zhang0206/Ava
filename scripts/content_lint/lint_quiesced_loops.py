"""Every resident periodic loop declares its stance on the stop window.

Run: `.venv/bin/python scripts/content_lint/lint_quiesced_loops.py [path ...]` (defaults to the
resident-service scope below; an explicit path that does not exist is an error
(stderr + exit 1) rather than a silent no-op). Also run automatically via
pre-commit hook.

## Why

`docs/decisions/runtime/processes/shutdown/2026-09-12-stop-window-contract.md` makes the window between a
completed drain and the release of the hold database-quiet: a paused runner's
idle pooled connections are what held PgBouncer's stop open. A loop that
borrows the pool every tick breaks that on its own, and nothing but the
author's memory made a new loop check `admission.quiesced()` — the audit found
most database-writing loops did not.

The shared round loop (`base/daemon/round_loop.py:run_rounds`) checks it
centrally, so a loop built on it needs nothing here. This lint covers the
hand-written ones.

## The rule

A resident loop — `while True:` or `while not <event>.is_set():`, with a
periodic wait in its own body (a call whose name contains `sleep`, or a
`wait`/`wait_for` call with a timeout), not nested inside another such loop of
the same function — must do one of:

- be gated: its enclosing function consults `admission.quiesced()` (or
  `admission.in_stop_leg()`, the narrower slice the host's turn scan reads), so
  a held unit skips the round and keeps beating its liveness;
- carry `# quiesce-exempt: <reason>` on the `while` line or the comment line right
  above it, naming why this loop needs no gate (it never touches the database, it is event-driven, or it
  deliberately runs through the window and the decision that says so).

A marker that no such loop starts on (or right below) is stale and fails too, so the
exemptions cannot outlive the code they describe. The marker is matched against
real comment tokens only.

## Limits

The check is lexical. It does not follow a call into another function to see
whether the gate guards the actual database work, and it does not see a
periodic task built without a `while` loop (`call_later`, a timer callback);
those are a reviewer's job, with the per-loop tests that pin "quiesced means no
pool borrow". It over-reports instead of under-reporting: a bounded retry loop
that matches the shape takes a marker with its reason.

Scope: non-test code of the resident services (`services/`, `gateway/`,
`ops/`).

Error format `file:line: <reason>` + non-zero exit.
"""

from __future__ import annotations

import ast
import io
import sys
import tokenize
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from scripts.structure import lint_common  # noqa: E402 - standalone script

_SCAN_DIRS = ("services", "gateway", "ops")

_MARKER = "quiesce-exempt:"
_GATES = frozenset({"quiesced", "in_stop_leg"})

_FIX = (
    "consult `admission.quiesced()` in this function so a held unit skips the round, or put "
    f"`# {_MARKER} <reason>` on or right above the `while` line when the loop needs no gate"
)

Function = ast.FunctionDef | ast.AsyncFunctionDef


def _callee_name(func: ast.expr) -> str:
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


def _own_nodes(node: ast.AST) -> list[ast.AST]:
    """Descendants of `node` outside any nested function, lambda or class."""
    found: list[ast.AST] = []
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda | ast.ClassDef):
            continue
        found.append(child)
        found.extend(_own_nodes(child))
    return found


def _resident_shape(loop: ast.While) -> bool:
    test = loop.test
    if isinstance(test, ast.Constant) and test.value is True:
        return True
    return (
        isinstance(test, ast.UnaryOp)
        and isinstance(test.op, ast.Not)
        and isinstance(test.operand, ast.Call)
        and _callee_name(test.operand.func) == "is_set"
    )


def _waits_periodically(loop: ast.While) -> bool:
    for node in _own_nodes(loop):
        if not isinstance(node, ast.Call):
            continue
        name = _callee_name(node.func)
        if "sleep" in name:
            return True
        if name in {"wait", "wait_for"} and any(kw.arg == "timeout" for kw in node.keywords):
            return True
    return False


def _gated(function: Function) -> bool:
    return any(
        (isinstance(node, ast.Attribute) and node.attr in _GATES)
        or (isinstance(node, ast.Name) and node.id in _GATES)
        for node in ast.walk(function)
    )


def _marker_lines(text: str) -> set[int]:
    """Lines whose real comment carries a marker with a non-empty reason."""
    try:
        return {
            tok.start[0]
            for tok in tokenize.generate_tokens(io.StringIO(text).readline)
            if tok.type == tokenize.COMMENT
            and _MARKER in tok.string
            and tok.string.split(_MARKER, 1)[1].strip()
        }
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return set()


def violations_in_source(src: str) -> list[tuple[int, str]]:
    """`[(lineno, reason), ...]` for one module's source — the unit-testable core."""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []  # ruff / py_compile own syntax
    markers = _marker_lines(src)
    resident: dict[int, ast.While] = {}
    gated_loops: set[int] = set()

    def visit(node: ast.AST, function: Function | None, *, inside_resident: bool) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                visit(child, child, inside_resident=False)
                continue
            nested = inside_resident
            if (
                isinstance(child, ast.While)
                and _resident_shape(child)
                and _waits_periodically(child)
            ):
                nested = True
                if not inside_resident:
                    resident[child.lineno] = child
                    if function is not None and _gated(function):
                        gated_loops.add(child.lineno)
            visit(child, function, inside_resident=nested)

    visit(tree, None, inside_resident=False)

    violations: list[tuple[int, str]] = []
    used: set[int] = set()
    for lineno in sorted(resident):
        marker = next((at for at in (lineno, lineno - 1) if at in markers), None)
        if marker is not None:
            used.add(marker)
        if marker is None and lineno not in gated_loops:
            violations.append((lineno, f"resident loop without a stop-window stance: {_FIX}"))
    violations.extend(
        (lineno, f"stale `{_MARKER}` marker: no resident loop starts on or right below this line")
        for lineno in sorted(markers - used)
    )
    return sorted(violations)


def _scan_file(path: Path) -> list[tuple[int, str]]:
    text = lint_common.read_utf8_text(path)
    return [] if text is None else violations_in_source(text)


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
    argv, only = lint_common.split_only(argv)
    if argv:
        missing = [arg for arg in argv if not Path(arg).exists()]
        if missing:
            print(f"error: target path(s) not found: {', '.join(missing)}", file=sys.stderr)
            return 1
    targets = (
        [Path(a).resolve() for a in argv]
        if argv
        else lint_common.scan_roots(_REPO_ROOT, _SCAN_DIRS)
    )
    scope = lint_common.changed_scope(only, _REPO_ROOT)

    total = 0
    for path in sorted(lint_common.restrict(_iter_py_files(targets), scope, _REPO_ROOT)):
        try:
            rel = path.relative_to(_REPO_ROOT).as_posix()
        except ValueError:
            rel = path.as_posix()
        if lint_common.is_test_path(rel):
            continue
        for lineno, reason in _scan_file(path):
            total += 1
            print(f"{rel}:{lineno}: {reason}.")

    if total:
        print(
            f"\n{total} violation(s). See the docstring at the top of "
            "scripts/content_lint/lint_quiesced_loops.py for the rule and what it does not see.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
