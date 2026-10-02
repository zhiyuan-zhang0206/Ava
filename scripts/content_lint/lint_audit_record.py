"""Every category=audit event must be recorded in Postgres before it is emitted.

Run: `.venv/bin/python scripts/content_lint/lint_audit_record.py [path ...]` (no path = full
scan of the framework dirs; `--only FILE...` judges changed files, the commit
hook form; `--print-baseline` prints the current census). Also a pre-commit hook.

## Why

`audit_events` is the system of record for audit facts (decisions/
2026-10-02-audit-events-in-postgres.md); Loki holds a projection that sheds,
truncates and expires. A call site that builds or enqueues an audit event
without going through `record_audit` / `record_audit_standalone`
(`base/telemetry/audit_events.py`) silently produces a fact that exists only in
the projection. No test notices: the event still shows up in Loki for 84 hours.

## The rule

Per function (the innermost enclosing def; module level counts as one unit), in
non-test code:

- a call to the enqueue-only API `insert_event_log` / `insert_event_log_async` /
  `insert_event_log_many` is a violation;
- a call that builds an audit event, `prepare_event_log(...)` or
  `prepare_event("audit", ...)` / `emit("audit", ...)`, is a violation unless the
  same function also calls `record_audit` or `record_audit_standalone`.

`base/telemetry/audit_events.py` (the recording primitives) and
`base/telemetry/emitter.py` (the event pipeline) are the only exempt files.

## Frozen baseline

The call sites that predate the primitive are frozen in `_BASELINE` below as an
exact `path::qualname -> number of violating calls` map: a new violation fails,
and so does a fixed one that is still listed. Migrating a site deletes its key
in the same change. The map is empty when every audit emit site records first.

The check is per function and per call name: it proves a function that builds an
audit event also records one, not that the recorded event is the emitted one.
That last link is covered by the recording primitives' tests.
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from base.host.env.dotenv_boot import enter_scratch_home  # noqa: E402

if __name__ == "__main__":
    enter_scratch_home()

from scripts.structure import lint_common  # noqa: E402 - standalone script

_SCAN_DIRS = lint_common.FRAMEWORK_DIRS

# Audit emit sites that predate record_audit / record_audit_standalone (frozen, shrink-only).
_BASELINE: dict[str, int] = {
    "agent/hooks/compact.py::auto_compact_for_llm": 1,
    "agent/ownership/corpse_reap.py::reap_crash_corpses": 1,
    "agent/ownership/corpse_reap.py::reap_recrashed_corpse": 1,
    "agent/ownership/hosted.py::admit_hosted_runtime": 2,
    "agent/ownership/hosted.py::settle_hosted_runtime": 1,
    "agent/turn/runloop.py::_handle_fatal_llm_error": 1,
    "agent/turn/runloop.py::_record_permanent_reject_outcome": 1,
    "ava/self.py::compact": 1,
    "ava/skills.py::_insert_skill_events": 1,
    "ava_builtins/plugins/ava_fleet/_task_update.py::_log_task_update": 1,
    "ava_builtins/plugins/ava_fleet/plugin.py::set_label": 1,
    "ava_builtins/plugins/ava_fleet/task_registry.py::_insert_task": 1,
    "base/agents/messages/chat_delivery.py::_insert_chat_inbound_once": 1,
    "base/db/__init__.py::announce_spawn_prompt": 1,
    "base/db/__init__.py::insert_compact_request_inbound": 1,
    "base/db/__init__.py::insert_inbound_message": 1,
    "base/db/__init__.py::insert_restart_completed_inbound": 1,
    "base/host/env/audit.py::_emit_audit_event": 1,
    "gateway/mcp_server/endpoint.py::_AuditMiddleware.__call__": 2,
    "ops/agents/spawn.py::_announce_created_agent": 1,
    "ops/agents/wake.py::_stage_resurrect_event": 1,
    "ops/lifecycle/__init__.py::_recover_crash_marked_blocking": 1,
    "ops/lifecycle/billing_recovery.py::_record_run_event": 1,
    "ops/lifecycle/termination.py::_stage_termination_event": 1,
    "services/computer/mcp_daemon.py::ComputerMcpDaemon._emit_action": 1,
    "services/computer/mcp_daemon.py::ComputerMcpDaemon._emit_session_event": 1,
}

_EXEMPT_FILES = frozenset({"base/telemetry/audit_events.py", "base/telemetry/emitter.py"})
_ENQUEUE_ONLY = frozenset({"insert_event_log", "insert_event_log_async", "insert_event_log_many"})
_CONSTRUCTORS = frozenset({"prepare_event", "emit"})
_RECORDERS = frozenset({"record_audit", "record_audit_standalone"})
_LEGACY_REASON = (
    "enqueue-only audit emit; record the event with record_audit / record_audit_standalone "
    "(base/telemetry/audit_events.py) so Postgres, not Loki, holds the fact"
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
        self.legacy: dict[str, list[int]] = {}
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
        name = _call_name(node)
        if name is not None:
            scope = self._scope()
            if name in _ENQUEUE_ONLY:
                self.legacy.setdefault(scope, []).append(node.lineno)
            elif name in _RECORDERS:
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
    for scope, lines in scan.legacy.items():
        found.extend((scope, line, _LEGACY_REASON) for line in lines)
    for scope, lines in scan.builds.items():
        if scope not in scan.records:
            found.extend((scope, line, _UNRECORDED_REASON) for line in lines)
    return sorted(found, key=lambda item: (item[1], item[0]))


def census(paths: list[Path]) -> tuple[dict[str, int], list[tuple[str, str]]]:
    """The `path::qualname -> count` map of the given files, plus `(key, line)` per call."""
    counts: dict[str, int] = {}
    detail: list[tuple[str, str]] = []
    for path in sorted(paths):
        rel = lint_common._rel_or_abs(path, _REPO_ROOT)
        if rel in _EXEMPT_FILES or lint_common.is_test_path(rel):
            continue
        text = lint_common.read_utf8_text(path)
        if text is None:
            continue
        for scope, lineno, reason in violations_in_source(text):
            key = f"{rel}::{scope}"
            counts[key] = counts.get(key, 0) + 1
            detail.append((key, f"{rel}:{lineno}: {scope}: {reason}"))
    return counts, detail


def _py_files(roots: list[Path]) -> list[Path]:
    files: list[Path] = []
    for root in roots:
        if root.is_file() and root.suffix == ".py":
            files.append(root)
        elif root.is_dir():
            files.extend(root.rglob("*.py"))
    return files


def compare(
    found: dict[str, int], baseline: dict[str, int], scope: frozenset[str] | None
) -> list[str]:
    """Problems between the census and the frozen baseline, restricted to `scope` files."""

    def in_scope(key: str) -> bool:
        return scope is None or key.split("::", 1)[0] in scope

    problems: list[str] = []
    for key in sorted(set(found) | set(baseline)):
        if not in_scope(key):
            continue
        have, frozen = found.get(key, 0), baseline.get(key, 0)
        if have > frozen:
            problems.append(f"{key}: {have} violating call(s), baseline allows {frozen}")
        elif have < frozen:
            problems.append(
                f"{key}: baseline lists {frozen} violating call(s) but only {have} remain; "
                "delete or lower the key in _BASELINE"
            )
    return problems


def _explicit_targets(argv: list[str]) -> list[Path] | None:
    """The explicit path arguments, or None when one does not exist (already reported)."""
    missing = [arg for arg in argv if not Path(arg).exists()]
    if missing:
        print(f"error: target path(s) not found: {', '.join(missing)}", file=sys.stderr)
        return None
    return [Path(a).resolve() for a in argv]


def _report(problems: list[str], detail: list[tuple[str, str]]) -> int:
    flagged = {problem.split(": ", 1)[0] for problem in problems}
    for key, line in detail:
        if key in flagged:
            print(line)
    for problem in problems:
        print(problem)
    print(
        f"\n{len(problems)} problem(s). See the docstring at the top of "
        "scripts/content_lint/lint_audit_record.py.",
        file=sys.stderr,
    )
    return 1


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    argv, only = lint_common.split_only(argv)
    print_baseline = "--print-baseline" in argv
    argv = [a for a in argv if a != "--print-baseline"]
    explicit = _explicit_targets(argv)
    if explicit is None:
        return 1
    targets = explicit or lint_common.scan_roots(_REPO_ROOT, _SCAN_DIRS)
    scope = lint_common.changed_scope(only, _REPO_ROOT)
    files = lint_common.restrict(_py_files(targets), scope, _REPO_ROOT)
    found, detail = census(files)
    if print_baseline:
        print(json.dumps(dict(sorted(found.items())), indent=2))
        return 0
    judged = (
        None
        if not argv and scope is None
        else frozenset(lint_common._rel_or_abs(f, _REPO_ROOT) for f in files)
    )
    problems = compare(found, _BASELINE, judged)
    return _report(problems, detail) if problems else 0


if __name__ == "__main__":
    sys.exit(main())
