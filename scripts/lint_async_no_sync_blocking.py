"""Lint: no synchronous blocking calls inside `async def` bodies (gateway + ops).

The gateway is a single event loop; one sync psycopg / subprocess /
psutil call in an async handler freezes every other request (the 2026-08-03
incident: `/api/memory/search` ran a synchronous gemini-embedding on the loop
and the gateway went unresponsive for hours). This lint makes that class a
commit-time error.

What it flags — any of these inside an `async def` (not inside a nested
`def` — a sync helper is presumed to be threaded):

- `with ...connection()` / `conn.cursor()` and direct `execute` /
  `fetchone` / `fetchall` / `commit` / `rollback` calls
- known sync DB helpers (`insert_inbound_message`, `get_agent_status`,
  `agent_exists`, `publish_agent_updated_sync`, ...)
- sync ops (`config_read_op`, `cluster_rollout_op`, `spawn_agent`, ...)
- process/session backends (`kill_session`, `has_session`, `force_kill`,
  `process_alive`, `capture_pane`)
- filesystem (`shutil.rmtree`, `Path.write_bytes/read_text`, ...)
- `shutil.` / `subprocess.` / `psutil.` module calls, `os.system` etc.
- `time.sleep`
- any direct call to a `*_blocking` helper — those exist to be wrapped in
  `asyncio.to_thread`, so a bare call is an unthreaded sync block by design.

A line that genuinely needs a sync call in an async handler (a tiny, bounded
read; a third-party callback contract) opts out with an inline
`# async-blocking-ok: <reason>` comment.

Scope: `gateway/` and `ops/` — the event-loop surfaces. Tests, cli, shared,
agent and services are not scanned (they do not run the gateway loop).
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_SCAN_DIRS = ("gateway", "ops")

# Sync callable attribute-names that must not appear un-awaited in an async body.
_BLOCKING_NAMES = {
    # psycopg surface
    "connection",
    "cursor",
    "execute",
    "fetchone",
    "fetchall",
    "commit",
    "rollback",
    # time
    "sleep",
    # DB helpers (sync psycopg on a fresh connection)
    "insert_inbound_message",
    "publish_agent_updated_sync",
    "agent_exists",
    "get_agent_status",
    "get_agent_machine",
    "latest_checkpoint_id",
    "list_open_page_names",
    "list_open_pages",
    "list_all_open_pages",
    "close_page",
    "register_page",
    "get_open_page_target",
    "close_all_agent_pages",
    "lookup_role",
    "validate_model_config",
    "write_fields",
    # the cross-process registry lock (blocks the calling thread while another
    # process holds it; the gateway reaches it from a sync handler)
    "mutate",
    # sync ops (subprocess / .env)
    "config_read_op",
    "config_write_op",
    "spawn_agent",
    "resurrect_agent",
    "cluster_recover_op",
    "cluster_stopping_op",
    "cluster_rollout_op",
    "cluster_restart_op",
    "cluster_update_check_op",
    "cluster_update_op",
    # process / session backends
    "kill_session",
    "has_session",
    "capture_pane",
    "force_kill",
    "process_alive",
    # filesystem
    "rmtree",
    "write_bytes",
    "read_bytes",
    "write_text",
    "read_text",
}

# Module-qualified sync calls (any attribute of these modules).
_BLOCKING_MODULES = {"shutil", "subprocess", "psutil"}
# os.* function names that block (not os.environ / os.path metadata).
_OS_BLOCKING = {"system", "popen", "spawn", "fork", "wait", "kill", "remove", "unlink", "rename"}

_EXEMPT_COMMENT = "# async-blocking-ok"


class _Linter(ast.NodeVisitor):
    def __init__(self, path: Path) -> None:
        self.path = path
        self.errors: list[tuple[int, str]] = []
        self._lines = path.read_text().splitlines()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        for stmt in node.body:
            self._walk(stmt)
        # Do NOT recurse into nested defs via generic_visit — a sync helper
        # nested inside an async function is presumed threaded by its caller.

    def _walk(self, node: ast.AST) -> None:
        if isinstance(
            node,
            (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda, ast.Await),
        ):
            # nested def = presumed-threaded helper; Await = already async
            return
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.Await):
                continue
            if isinstance(child, ast.Call):
                self._check_call(child)
            elif isinstance(child, ast.With):
                for item in child.items:
                    if isinstance(item.context_expr, ast.Call):
                        self._check_call(item.context_expr)
            self._walk(child)

    def _check_call(self, node: ast.Call) -> None:
        func = node.func
        name: str | None = None
        if isinstance(func, ast.Attribute):
            name = func.attr
        elif isinstance(func, ast.Name):
            name = func.id
        if not name:
            return

        reason: str | None = None
        if name in _BLOCKING_NAMES:
            reason = f"sync call {name}()"
        elif name.endswith("_blocking"):
            reason = f"{name}() is a sync helper — must run via asyncio.to_thread"
        elif isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            if func.value.id in _BLOCKING_MODULES:
                reason = f"sync module call {func.value.id}.{name}()"
            elif func.value.id == "os" and name in _OS_BLOCKING:
                reason = f"os.{name}()"
        if reason is None:
            return

        line = node.lineno
        if 1 <= line <= len(self._lines) and _EXEMPT_COMMENT in self._lines[line - 1]:
            return
        snippet = ast.unparse(node)[:110].replace("\n", " ")
        self.errors.append((line, f"{reason}: {snippet}"))


def main() -> int:
    files = sorted(
        p for d in _SCAN_DIRS for p in (_ROOT / d).rglob("*.py") if "__pycache__" not in p.parts
    )
    all_errors: list[tuple[Path, list[tuple[int, str]]]] = []
    for path in files:
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:
            continue
        linter = _Linter(path)
        linter.visit(tree)
        if linter.errors:
            all_errors.append((path, linter.errors))

    if not all_errors:
        print(f"async-blocking lint: {len(files)} files clean")
        return 0
    for path, errors in all_errors:
        print(f"{path.relative_to(_ROOT)}:")
        for line, reason in errors:
            print(f"  {line}: {reason}")
    print(
        f"\n{sum(len(e) for _, e in all_errors)} blocking call(s) in async bodies "
        f"across {len(all_errors)} file(s)."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
