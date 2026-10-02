"""Every category=audit event must be recorded in Postgres before it is emitted.

Run: `.venv/bin/python scripts/content_lint/lint_audit_record.py [path ...]` (no path = full
scan of the framework dirs; `--only FILE...` judges changed files, the commit
hook form). Also a pre-commit hook.

## Why

`audit_events` is the system of record for audit facts (decisions/
2026-10-02-audit-events-in-postgres.md); Loki holds a projection that sheds,
truncates and expires. A call site that builds an audit event without
recording it through one of the `record_audit*` primitives
(`base/telemetry/audit_events.py`) silently produces a fact that exists only in
the projection. No test notices: the event still shows up in Loki for 84 hours.

## The rule

Per function (the innermost enclosing def; module level counts as one unit), in
non-test code:

- a call that builds an audit event, `prepare_event_log(...)` or
  `prepare_event("audit", ...)` / `emit("audit", ...)`, is a violation unless the
  same function also calls (or passes to a runner such as `asyncio.to_thread`) a
  recorder: `record_audit*` (see
  `base/telemetry/audit_events.py`) or `emit_recorded_central_event` (the
  service-owned wrapper that records).

`base/telemetry/audit_events.py` (the recording primitives) and
`base/telemetry/emitter.py` (the event pipeline) are the only exempt files.

## Sites whose record is elsewhere

`_LOCAL_RECORD_SITES` lists the few emit sites whose record is deliberately not
`audit_events`, each with its reason; they are skipped.

The check is per function and per call name: it proves a function that builds an
audit event also records one, not that the recorded event is the emitted one.
That last link is covered by the recording primitives' tests.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from base.host.env.dotenv_boot import enter_scratch_home  # noqa: E402

if __name__ == "__main__":
    enter_scratch_home()

from scripts.structure import lint_common  # noqa: E402 - standalone script

_SCAN_DIRS = lint_common.FRAMEWORK_DIRS

# Sites whose audit record is deliberately not `audit_events`: key `path::function`, value the
# reason. The `.env` write audit's record is the per-home JSONL because its writers run where
# no database identity exists (decisions/2026-10-02-env-write-audit-stays-local.md).
_LOCAL_RECORD_SITES: dict[str, str] = {
    "base/host/env/audit.py::_emit_audit_event": "the per-home .env audit JSONL is the record",
}

_EXEMPT_FILES = frozenset({"base/telemetry/audit_events.py", "base/telemetry/emitter.py"})
_CONSTRUCTORS = frozenset({"prepare_event", "emit"})
_RECORDERS = frozenset(
    {
        "record_audit",
        "record_audit_async",
        "record_audit_standalone",
        "record_audit_standalone_async",
        "record_audit_standalone_many",
        "record_audit_reported",
        "record_audit_reported_async",
        "emit_recorded_central_event",
    }
)
_UNRECORDED_REASON = (
    "builds an audit event but never calls record_audit / record_audit_standalone in the "
    "same function"
)


def _call_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def _reference_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _builds_audit_event(node: ast.Call, name: str) -> bool:
    if name == "prepare_event_log":
        return True
    if name not in _CONSTRUCTORS or not node.args:
        return False
    first = node.args[0]
    return isinstance(first, ast.Constant) and first.value == "audit"


class _Scan(ast.NodeVisitor):
    """Collect, per function qualname, the calls the rule cares about."""

    def __init__(self) -> None:
        self.stack: list[str] = []
        self.builds: dict[str, list[int]] = {}
        self.records: set[str] = set()

    def _scope(self) -> str:
        return ".".join(self.stack) or "<module>"

    def _visit_def(self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> None:
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_def(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_def(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._visit_def(node)

    def visit_Call(self, node: ast.Call) -> None:
        if any(_reference_name(arg) in _RECORDERS for arg in node.args):
            # A recorder handed to a runner, e.g. `asyncio.to_thread(record_audit_reported, event)`.
            self.records.add(self._scope())
        name = _call_name(node)
        if name is not None:
            scope = self._scope()
            if name in _RECORDERS:
                self.records.add(scope)
            elif _builds_audit_event(node, name):
                self.builds.setdefault(scope, []).append(node.lineno)
        self.generic_visit(node)


def violations_in_source(src: str) -> list[tuple[str, int, str]]:
    """`[(qualname, lineno, reason), ...]` for one module's source."""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []  # ruff / py_compile own syntax errors
    scan = _Scan()
    scan.visit(tree)
    found: list[tuple[str, int, str]] = []
    for scope, lines in scan.builds.items():
        if scope not in scan.records:
            found.extend((scope, line, _UNRECORDED_REASON) for line in lines)
    return sorted(found, key=lambda item: (item[1], item[0]))


def census(paths: list[Path]) -> list[str]:
    """One `file:line: scope: reason` line per violating call in the given files."""
    found: list[str] = []
    for path in sorted(paths):
        rel = lint_common._rel_or_abs(path, _REPO_ROOT)
        if rel in _EXEMPT_FILES or lint_common.is_test_path(rel):
            continue
        text = lint_common.read_utf8_text(path)
        if text is None:
            continue
        for scope, lineno, reason in violations_in_source(text):
            if f"{rel}::{scope}" not in _LOCAL_RECORD_SITES:
                found.append(f"{rel}:{lineno}: {scope}: {reason}")
    return found


def _py_files(roots: list[Path]) -> list[Path]:
    files: list[Path] = []
    for root in roots:
        if root.is_file() and root.suffix == ".py":
            files.append(root)
        elif root.is_dir():
            files.extend(root.rglob("*.py"))
    return files


def _explicit_targets(argv: list[str]) -> list[Path] | None:
    """The explicit path arguments, or None when one does not exist (already reported)."""
    missing = [arg for arg in argv if not Path(arg).exists()]
    if missing:
        print(f"error: target path(s) not found: {', '.join(missing)}", file=sys.stderr)
        return None
    return [Path(a).resolve() for a in argv]


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    argv, only = lint_common.split_only(argv)
    explicit = _explicit_targets(argv)
    if explicit is None:
        return 1
    targets = explicit or lint_common.scan_roots(_REPO_ROOT, _SCAN_DIRS)
    scope = lint_common.changed_scope(only, _REPO_ROOT)
    found = census(lint_common.restrict(_py_files(targets), scope, _REPO_ROOT))
    for line in found:
        print(line)
    if found:
        print(
            f"\n{len(found)} audit event(s) built without being recorded. See the docstring at "
            "the top of scripts/content_lint/lint_audit_record.py.",
            file=sys.stderr,
        )
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
