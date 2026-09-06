"""Structural lints that keep the codebase legible to agents: no `TYPE_CHECKING`
import-folding, and a per-file line ceiling.

Run: `.venv/bin/python scripts/lint_code_structure.py [path ...]` (defaults to the
whole repo). Also run automatically via pre-commit hook before commit.

## Why

A fully agent-generated codebase is optimized first for the agent's ability to
reason over it directly. Two structural rules protect that:

### Rule 1: no `if TYPE_CHECKING:` import folding

All dependencies are imported at top level. Reasons (see AGENTS.md "Python
coding conventions"): an application's deps are always on the runtime path, so
the startup-cost TYPE_CHECKING saves does not exist (the module is already
cached); and LangGraph / Pydantic introspect type hints at runtime via
`get_type_hints()` / `inspect.signature`, so a name imported only under
`TYPE_CHECKING` raises NameError. The genuine exceptions — a real circular
import, or an `import torch`-class heavy dependency on a path that does not use
the type — go in `_TYPE_CHECKING_ALLOWED` with a one-line reason.

### Rule 3: `machine_role()` allowlist

`machine_role()` answers "what does this host serve" and must never be used to
decide *where an operation runs* — the gateway is the single routing point, and
every CLI routes to it (user ruling 2026-08-21, issue #216). The line is not
*where* the function is called but *what the answer is used for*:

- **legitimate** — "what do I serve?": start the right daemons, advertise
  honestly in register_self, audit this host, guard a capability.
- **illegitimate** — "should I do this myself, or ask the gateway?": a module
  that implements an operation must not branch on role.

So instead of banning the call (five legitimate uses exist) we allowlist it:
`_MACHINE_ROLE_ALLOWED` enumerates the modules that may call `machine_role()`,
each with a one-line reason naming the question it answers. A call anywhere
else fails the run; an allowlisted module that stops calling it also fails
(stale-entry alert, the `unmatched_ignore_imports_alerting` shape from #176)
so the list cannot rot into a permission wall.

### Rule 2: per-file line budget (600 soft / 800 hard)

600-800 lines is a transitional zone: tolerated, but surfaced on every full run
as a nudge to split. Past 800 it is a hard error — a file that large is hard for
an agent to hold in context and reason about as a unit. There is no exemption:
split into focused modules.

Scope (`_SCAN_DIRS`) tracks `[tool.importlinter] root_packages` in pyproject.toml,
so a new governed package is gated the moment it is declared a layer.

Hard violations (TYPE_CHECKING, over-800) print `file:line: <remediation>` and
fail the run; transitional-zone files print a note to stderr and do not fail.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent

_TRANSITIONAL_FLOOR = 600
_HARD_CEILING = 800

# Core source held to the line budget — scripts/migrations/db/tests
# are not scanned.
#
# Kept in step with `[tool.importlinter] root_packages` in pyproject.toml, which
# is the repo's standing list of governed source packages. A package that is a
# layer there but absent here is silently ungated, and that is an easy hole to
# fall into: this tuple was written once (2026-05-28) and never revisited, so
# `ops/` — extracted out of the scanned `gateway/` a month later — carried its
# files out of the budget with it. `ava_builtins` (mcps + skills + plugins)
# entered 2026-08-07 (Task #1011) after its oversized files were split to the
# budget. Its `skills/*/reference/*.py` are agent-facing reference scripts and
# `skills/*/vendor/` holds vendored third-party payloads, but only `*.py` is
# collected by `_iter_py_files` anyway — the vendored highlight.js under
# skills/ava-ui/widgets/markdown/vendor/ is outside the scan by construction.
_SCAN_DIRS = (
    "agent",
    "ava",
    "ava_builtins",
    "gateway",
    "shared",
    "services",
    "ops",
    "cli",
)

# Rule 3 allowlist — modules that may call machine_role(), each with the
# question the call answers ("what do I serve" vs "where does this run").
# A call site not listed here fails the lint; a listed module whose calls
# disappear fails too (stale entry). See the module docstring.
_MACHINE_ROLE_ALLOWED: dict[str, str] = {
    "cli/commands/_maintenance.py": "Which services/data plane does this explicitly local, DB-offline-capable stop/start own? Fleet transport is operator-coordinated.",
    "shared/machine.py": "defines machine_role() and its capability wrappers is_gateway()/is_agent_runner() — the implementation itself",
    "shared/observability.py": "does this process serve the gateway capability whose LGTM marker governs telemetry (what do I serve)",
    "services/healthchecks/otel_collector.py": "does this unit own the LGTM collector healthcheck, preserving pure-runner relay behavior (what do I serve)",
    "cli/commands/start.py": "which daemons do I bring up (what do I serve)",
    "cli/commands/_repo.py": "resolve this host's capability set, None when unset, for stop/status/converge (what do I serve)",
    "cli/commands/_gateway_ready.py": "audit this host's role for the readiness report (what do I serve)",
    "cli/commands/trace.py": "which recovery ingress does this host serve: gateway-local Tempo or a pure-runner relay target (what do I serve)",
    "services/agent_ops/_boot.py": "what do I advertise in register_self (what do I serve)",
    "ops/ops_inventory.py": "capability guard: inventory ops are agent-runner-only (what do I serve)",
    "gateway/routers/config.py": "for the gateway itself, local role is authoritative (what do I serve)",
    "cli/commands/_release_inventory.py": "verified installed image reads the real annotated service roster for the unit receipt (read-only, WHEEL_RUNTIME-guarded; no serve decision)",
}


def _machine_role_calls(tree: ast.AST) -> list[int]:
    """Line numbers of `machine_role(...)` call sites in a parsed module."""
    hits: list[int] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "machine_role"
        ):
            hits.append(node.lineno)
    return hits


# Files allowed to use `if TYPE_CHECKING:` — real circular import or heavy
# optional dependency. Empty today; add an entry with a one-line reason.
_TYPE_CHECKING_ALLOWED: frozenset[str] = frozenset()


def _type_checking_violations(tree: ast.Module) -> list[int]:
    """Line numbers of `if TYPE_CHECKING:` / `if typing.TYPE_CHECKING:` blocks."""
    hits: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        name = (
            test.id
            if isinstance(test, ast.Name)
            else test.attr
            if isinstance(test, ast.Attribute)
            else None
        )
        if name == "TYPE_CHECKING":
            hits.append(node.lineno)
    return hits


def _scan_file(path: Path, rel_path: str) -> list[tuple[int, str, str]]:
    """Return [(lineno, message, severity), ...]; severity is "error" | "note"."""
    text = path.read_text(encoding="utf-8")
    out: list[tuple[int, str, str]] = []

    tree = ast.parse(text, filename=rel_path)

    if rel_path not in _TYPE_CHECKING_ALLOWED:
        for lineno in _type_checking_violations(tree):
            out.append(
                (
                    lineno,
                    "`if TYPE_CHECKING:` is banned — import at top level. "
                    "App deps are always on the runtime path and LangGraph/Pydantic "
                    "introspect type hints at runtime (deferred imports NameError). "
                    "Real circular-import / heavy-dep cases: refactor, or add this file "
                    "to _TYPE_CHECKING_ALLOWED in scripts/lint_code_structure.py with a reason.",
                    "error",
                )
            )

    role_calls = _machine_role_calls(tree)
    if rel_path in _MACHINE_ROLE_ALLOWED:
        if not role_calls:
            out.append(
                (
                    1,
                    f"stale machine_role() allowlist entry — {rel_path} no longer calls "
                    "machine_role(); remove it from _MACHINE_ROLE_ALLOWED in "
                    "scripts/lint_code_structure.py (the list must match reality, "
                    "issue #216).",
                    "error",
                )
            )
    elif role_calls:
        for lineno in role_calls:
            out.append(
                (
                    lineno,
                    "machine_role() may only be called from modules in "
                    "_MACHINE_ROLE_ALLOWED (scripts/lint_code_structure.py) — the "
                    "gateway is the single routing point and no operation may branch "
                    "on role (user ruling 2026-08-21, issue #216). Add the module "
                    "deliberately with the question the call answers, or route the "
                    "operation to the gateway instead.",
                    "error",
                )
            )

    n_lines = len(text.splitlines())
    if n_lines > _HARD_CEILING:
        out.append(
            (
                n_lines,
                f"file is {n_lines} lines, over the {_HARD_CEILING}-line hard ceiling — "
                "split into focused modules.",
                "error",
            )
        )
    elif n_lines > _TRANSITIONAL_FLOOR:
        out.append(
            (
                n_lines,
                f"file is {n_lines} lines, in the {_TRANSITIONAL_FLOOR}-{_HARD_CEILING} "
                "transitional zone — consider splitting before it hits the hard ceiling.",
                "note",
            )
        )
    return out


def _in_scan_scope(rel_path: str) -> bool:
    return any(rel_path == d or rel_path.startswith(f"{d}/") for d in _SCAN_DIRS)


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
    # argv non-empty = pre-commit passed changed files; empty = full scan of _SCAN_DIRS.
    targets = [Path(a).resolve() for a in argv] if argv else [_REPO_ROOT / d for d in _SCAN_DIRS]

    errors = 0
    notes: list[str] = []
    for path in sorted(_iter_py_files(targets)):
        try:
            rel = path.relative_to(_REPO_ROOT).as_posix()
        except ValueError:
            rel = path.as_posix()
        if not _in_scan_scope(rel):
            continue
        for lineno, message, severity in _scan_file(path, rel):
            if severity == "error":
                errors += 1
                print(f"{rel}:{lineno}: {message}")
            else:
                notes.append(f"{rel}:{lineno}: {message}")

    if notes:
        print(f"\n{len(notes)} file(s) in the transitional zone (not blocking):", file=sys.stderr)
        for note in notes:
            print(f"  {note}", file=sys.stderr)

    if errors:
        print(
            f"\n{errors} hard violations. See the docstring at the top of "
            "scripts/lint_code_structure.py for the rules and exemption procedure.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
