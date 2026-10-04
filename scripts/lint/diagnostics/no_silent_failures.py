"""Forbid broad exception handlers that swallow a failure without a trace.

Run: `.venv/bin/python scripts/lint/diagnostics/no_silent_failures.py [path ...]` (defaults to
the framework dirs plus `scripts/`, `schedules/` and `.agents/`; tests are not
scanned; an explicit path that does not exist is an error (stderr + exit 1)
rather than a silent no-op). Also run automatically via pre-commit hook
(`--only` changed files).

## Why

A broad handler that neither re-raises nor reports turns every bug in its `try`
body into "nothing happened". The `ava_code` after_exec hook read per-turn state
from the host process, raised `PluginStateOutsideTurnError` on every call, and a
`contextlib.suppress(Exception)` ate it: the hook was a no-op from the day it
shipped and there was not one log line to say so. The failure a handler hides is
indistinguishable from a feature that works.

## The rule

Two shapes are flagged, in production code:

1. `contextlib.suppress(...)` / `suppress(...)` naming `Exception` or
   `BaseException` (alone or inside a tuple).
2. An `except` handler for `Exception`, `BaseException`, a tuple containing
   either, or a bare `except:` whose body does none of:
   - `raise` (any form: re-raise, wrap, translate);
   - call a WARNING-or-above log method (`.warning` / `.warn` / `.error` /
     `.critical` / `.exception` / `.fatal`, on any receiver; `.log("WARNING"
     | "ERROR" | "CRITICAL", ...)`), or a telemetry `emit(...)` /
     `emit_prepared(...)`;
   - write to stderr (`print(..., file=...)`, `sys.stderr.write(...)`) or
     `sys.exit(...)` — the report channel of a standalone script;
   - call `.handleError(record)` — a stdlib `logging.Handler`'s own failure
     channel, which prints the traceback to stderr;
   - hand the bound exception on (`except Exception as exc:` and `exc` is read
     anywhere in the body outside a `debug` / `info` / `trace` / `success` log
     call): returned in an error result, appended to a failure list,
     `set_exception`, `add_note`, passed to a reporter. A debug or info line
     is not a report: it is below every production sink's threshold.

Nested function / class bodies and lambdas inside the handler do not count: a
raise or log defined there does not run when the handler does.

What to do about a hit, in order of preference: narrow the handler to the
exceptions the `try` body can legitimately raise (an expected condition may be
handled quietly: `FileNotFoundError`, `ProcessLookupError`,
`asyncio.CancelledError` ...); delete the handler and let the failure surface;
keep the broad handler and log it at WARNING with the traceback
(`logger.opt(exception=True).warning(...)`) or emit a structured event.

## Exemption

`# silent-ok: <reason>` on the `except ...:` / `with suppress(...):` line (or any
line of a multi-line header). The reason is required and is read by reviewers:
the legitimate cases are the ones where reporting is impossible or would recurse
— a log sink's own failure path, the telemetry pipeline reporting its own loss.
There is no allowlist file; every exemption is visible at its site.

Error format `file:line: <message>` + non-zero exit.
"""

from __future__ import annotations

import ast
import re
import sys
from collections.abc import Iterator
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO_ROOT))

from scripts.structure import lint_common  # noqa: E402 - standalone script

_SCAN_DIRS = (*lint_common.FRAMEWORK_DIRS, "scripts", "schedules", ".agents")

_BROAD = frozenset({"Exception", "BaseException"})
_LOUD_LOG_METHODS = frozenset({"warning", "warn", "error", "critical", "exception", "fatal"})
_LOUD_LEVELS = frozenset({"WARNING", "WARN", "ERROR", "CRITICAL", "FATAL"})
_QUIET_LOG_METHODS = frozenset({"trace", "debug", "info", "success"})
_EMIT_NAMES = frozenset({"emit", "emit_prepared"})
_NESTED_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)
_MARKER_RE = re.compile(r"#\s*silent-ok:\s*(\S.*)$")


def _tail_name(node: ast.expr) -> str | None:
    """The final identifier of a Name / dotted Attribute (`contextlib.suppress` -> `suppress`)."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _broad_names(node: ast.expr | None) -> list[str]:
    """The broad exception names an `except` type / `suppress` argument names."""
    if node is None:
        return ["bare except"]
    elements = node.elts if isinstance(node, ast.Tuple) else [node]
    return [name for e in elements if (name := _tail_name(e)) in _BROAD]


def _walk_own_scope(stmts: list[ast.stmt]) -> Iterator[ast.AST]:
    """Every node of `stmts`, not descending into nested function / class / lambda bodies."""
    stack: list[ast.AST] = list(stmts)
    while stack:
        node = stack.pop()
        yield node
        if not isinstance(node, _NESTED_SCOPES):
            stack.extend(ast.iter_child_nodes(node))


def _is_stderr(node: ast.expr) -> bool:
    return isinstance(node, ast.Attribute) and node.attr == "stderr"


def _is_loud_log(call: ast.Call) -> bool:
    """A WARNING-or-above log call, or a telemetry emit / handleError."""
    func = call.func
    if isinstance(func, ast.Attribute) and func.attr in _LOUD_LOG_METHODS:
        return True
    if _tail_name(func) in _EMIT_NAMES | {"handleError"}:
        return True
    return (
        isinstance(func, ast.Attribute)
        and func.attr == "log"
        and bool(call.args)
        and isinstance(call.args[0], ast.Constant)
        and str(call.args[0].value).upper() in _LOUD_LEVELS
    )


def _is_stderr_report(call: ast.Call) -> bool:
    """A write to stderr, or `sys.exit(...)`: the report channel of a standalone script."""
    func = call.func
    if isinstance(func, ast.Name) and func.id == "print":
        return any(kw.arg == "file" and _is_stderr(kw.value) for kw in call.keywords)
    if isinstance(func, ast.Attribute) and func.attr == "write":
        return _is_stderr(func.value)
    return (
        isinstance(func, ast.Attribute) and func.attr == "exit" and _tail_name(func.value) == "sys"
    )


def _is_loud_call(call: ast.Call) -> bool:
    return _is_loud_log(call) or _is_stderr_report(call)


def _quiet_log_node_ids(body: list[ast.stmt]) -> set[int]:
    """ids of every node inside a debug / info / trace / success log call."""
    inside: set[int] = set()
    for node in _walk_own_scope(body):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in _QUIET_LOG_METHODS
        ):
            inside.update(id(n) for n in ast.walk(node))
    return inside


def _reports_failure(handler: ast.ExceptHandler) -> bool:
    """Does the handler re-raise, log at WARNING+, emit, write to stderr, or pass `exc` on?"""
    nodes = list(_walk_own_scope(handler.body))
    if any(isinstance(n, ast.Raise) for n in nodes):
        return True
    if any(isinstance(n, ast.Call) and _is_loud_call(n) for n in nodes):
        return True
    if handler.name is None:
        return False
    quiet = _quiet_log_node_ids(handler.body)
    return any(
        isinstance(n, ast.Name) and n.id == handler.name and id(n) not in quiet for n in nodes
    )


def _header_lines(node: ast.AST, body: list[ast.stmt], lines: list[str]) -> str:
    """Source text from the statement's own line to just before its first body line."""
    first = getattr(node, "lineno", 1)
    last = max(first, body[0].lineno - 1) if body else first
    return "\n".join(lines[first - 1 : last])


def _has_marker(node: ast.AST, body: list[ast.stmt], lines: list[str]) -> bool:
    return _MARKER_RE.search(_header_lines(node, body, lines).replace("\n", " ")) is not None


def _suppress_hits(node: ast.With | ast.AsyncWith) -> list[str]:
    hits: list[str] = []
    for item in node.items:
        expr = item.context_expr
        if isinstance(expr, ast.Call) and _tail_name(expr.func) == "suppress":
            hits.extend(name for arg in expr.args for name in _broad_names(arg))
    return hits


def violations_in_source(src: str, filename: str = "<source>") -> list[tuple[int, str]]:
    """Return [(lineno, message), ...] for every silent broad handler in `src`.

    Takes source rather than a path so the lint's own tests can drive it with
    literal snippets.
    """
    try:
        tree = ast.parse(src, filename=filename)
    except SyntaxError as exc:
        return [(exc.lineno or 1, f"could not parse: {exc}")]
    lines = src.splitlines()
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.With, ast.AsyncWith)):
            hits = _suppress_hits(node)
            if hits and not _has_marker(node, node.body, lines):
                out.append(
                    (
                        node.lineno,
                        f"`suppress({', '.join(hits)})` swallows every failure of its body "
                        "without a trace: suppress the specific exception, or catch broadly "
                        "and log at WARNING with the traceback",
                    )
                )
        elif isinstance(node, ast.ExceptHandler):
            hits = _broad_names(node.type)
            if hits and not _reports_failure(node) and not _has_marker(node, node.body, lines):
                out.append(
                    (
                        node.lineno,
                        f"`except {hits[0] if hits[0] != 'bare except' else ''}:` neither "
                        "re-raises nor reports (WARNING+ log with traceback, emit, or passing "
                        "the exception on): narrow it, drop it, or report it",
                    )
                )
    return sorted(out)


def _default_files() -> list[str]:
    roots = lint_common.scan_roots(_REPO_ROOT, _SCAN_DIRS)
    return sorted(
        rel
        for root in roots
        for path in root.rglob("*.py")
        if not _is_test_file(rel := path.relative_to(_REPO_ROOT).as_posix())
    )


def _is_test_file(rel: str) -> bool:
    name = rel.rsplit("/", 1)[-1]
    return lint_common.is_test_path(rel) or name.startswith("test_") or name == "conftest.py"


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    argv, only = lint_common.split_only(argv)
    if argv:
        targets, missing = lint_common.resolve_targets(argv, _REPO_ROOT)
        if missing:
            print(f"error: target path(s) not found: {', '.join(missing)}", file=sys.stderr)
            return 1
        files = [f for f in targets if not _is_test_file(f)]
    else:
        scope = lint_common.changed_scope(only, _REPO_ROOT)
        files = _default_files() if scope is None else [f for f in _default_files() if f in scope]

    total = 0
    for rel in files:
        text = lint_common.read_utf8_text(_REPO_ROOT / rel) if rel.endswith(".py") else None
        for lineno, message in violations_in_source(text, rel) if text is not None else ():
            total += 1
            print(f"{rel}:{lineno}: {message}")

    if total:
        print(
            f"\n{total} silent broad handler(s): narrow to the expected exception, let it "
            "fail, or log it at WARNING with the traceback; see the docstring at the top "
            "of scripts/lint/diagnostics/no_silent_failures.py.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
