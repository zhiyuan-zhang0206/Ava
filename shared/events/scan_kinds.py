# ruff: noqa: T201  # CLI tool — printing the inventory is the point.
"""scan_kinds.py — Reproducible event-name inventory for the Ava event registry.

Regenerates the raw material behind shared/events/registry.md:

  Terminology: `kind` is the legacy name — the unified event model names the
  field `event_name` (OTel `event.name`; shared/events/registry.md). The scanner
  output feeds that registry; static `event=` literals remain the carrier
  (emit's `event_name` argument is positional / variable, not scanned).

  1. Static `event=` literals in production Python code (agent_events event names).
  2. Static `label=` literals on logger calls (label fallback -> agent_events
     event names; see shared/log.py event resolution: event -> label -> "log").
  3. `insert_event_log*` event_type values (event_log event names, category=audit).
  4. SSE role discriminators in shared/live_events.py (real-time channel,
     not persisted).
  5. Optional: historical distribution from the PG archive (pass --db-url;
     read-only SELECT queries). ARCHIVE ONLY since the LGTM cutover (task
     #1197): `event_log` is frozen and the `agent_events` mirrors were removed
     (2026-08-06) — against a live cluster the DB scan errors or returns
     pre-cutover data; the live distribution lives in Loki (LogQL).

Usage:
    python shared/events/scan_kinds.py [--db-url postgresql://...] [--repo ~/Ava]

Stdlib-only unless --db-url is given (needs psycopg). Output is a
de-duplicated event-name inventory grouped by mechanism. shared/events/registry.md
is generated from the EVENTS registry (scripts/gen_event_registry.py), not from
this output; this tool remains useful to audit the event= literal distribution
across the codebase.
"""

from __future__ import annotations

import argparse
import ast
import os
import re
import sys
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

# Directories never scanned (mirrors the repo hygiene excludes).
EXCLUDE_DIRS = {
    ".git",
    ".claude",
    ".worktrees",
    "node_modules",
    ".next",
    "logs",
    "runs",
    "outputs",
    "tmp",
    "work",
    "dist",
    ".venv",
    "venv",
    "__pycache__",
    ".ruff_cache",
    ".pytest_cache",
    ".mypy_cache",
    ".pyright",
    "demos",
    "deploy",
    "web",
    "desktop",
    "dashboards",
}

EVENT_RE = re.compile(r"""\bevent\s*=\s*["']([^"']+)["']""")
LABEL_RE = re.compile(r"""\blabel\s*=\s*["']([^"']+)["']""")
EVENT_TYPE_RE = re.compile(r"""event_type\s*=\s*["']([^"']+)["']""")
SSE_ROLE_RE = re.compile(r'role: Literal\["([^"]+)"\]')


def iter_production_py(repo: Path) -> Iterator[tuple[str, Path]]:
    """Yield (relative_path, path) for every production .py file."""
    for dirpath, dirnames, filenames in os.walk(repo):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            path = Path(dirpath) / fn
            rel = str(path.relative_to(repo))
            if "/tests/" in rel or rel.startswith("tests/"):
                continue
            yield rel, path


def walk_py(repo: Path) -> Iterator[tuple[str, list[str]]]:
    """Yield (relative_path, lines) for every production .py file."""
    for rel, path in iter_production_py(repo):
        try:
            with path.open(encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except OSError:
            continue
        yield rel, lines


def scan_code(repo: Path) -> tuple[Counter[str], Counter[str], Counter[str], Counter[str]]:
    """Return (event_kinds, label_kinds, event_type_kinds, sse_roles)."""
    event_kinds: Counter[str] = Counter()
    label_kinds: Counter[str] = Counter()
    event_type_kinds: Counter[str] = Counter()
    sse_roles: Counter[str] = Counter()
    for _, lines in walk_py(repo):
        for line in lines:
            event_kinds.update(m.group(1) for m in EVENT_RE.finditer(line))
            label_kinds.update(m.group(1) for m in LABEL_RE.finditer(line))
            event_type_kinds.update(m.group(1) for m in EVENT_TYPE_RE.finditer(line))
    # PR-D renamed shared/events.py -> shared/live_events.py; keep both names so
    # the scanner works on pre-rename checkouts too (batch lands A -> ... -> E).
    events_path = repo / "shared" / "live_events.py"
    if not events_path.exists():
        events_path = repo / "shared" / "events.py"
    if events_path.exists():
        with events_path.open(encoding="utf-8", errors="replace") as f:
            for line in f:
                sse_roles.update(m.group(1) for m in SSE_ROLE_RE.finditer(line))
    return event_kinds, label_kinds, event_type_kinds, sse_roles


# ── label-only loguru record calls (AST) ────────────────────────────────────


@dataclass(frozen=True)
class LabelOnlyCall:
    """One loguru record call carrying `label=` and no `event=` in its chain.

    `label` is the literal value when the keyword argument is a plain string
    constant; None marks a non-literal (dynamic) expression whose registeredness
    cannot be proven statically — the label-only gate in
    tests/test_lint_event_kinds.py fails that closed.
    """

    path: str
    lineno: int
    label: str | None


# loguru record-level methods — the call that writes the record. `log` takes
# the level name as its first positional argument.
_LEVEL_METHODS = frozenset(
    {"trace", "debug", "info", "success", "warning", "error", "critical", "exception", "log"}
)

# Methods that return a derived logger — the only chain links this scan
# follows. loguru also has `.patch()`; unused in this repo, and a chain
# through anything else is simply not matched.
_CHAIN_METHODS = frozenset({"bind", "opt"})


def _logger_bindings(tree: ast.AST) -> tuple[set[str], set[str]]:
    """(names bound to a loguru logger, `shared.log` module aliases).

    Both `from shared.log import logger` and `from loguru import logger` bind
    the same singleton: shared.log configures its sinks on loguru's global
    logger, so a record emitted through either spelling resolves
    event -> label -> "log" identically — and an unregistered label raises in
    the sink the same way.
    """
    names: set[str] = set()
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module in ("loguru", "shared.log"):
                for alias in node.names:
                    if alias.name == "logger":
                        names.add(alias.asname or "logger")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in ("loguru", "shared.log"):
                    modules.add(alias.asname or alias.name)
    return names, modules


def _is_logger_root(node: ast.expr, names: set[str], modules: set[str]) -> bool:
    """Whether `node` is a raw logger expression.

    Accepted roots: a name imported from `loguru` / `shared.log` (incl. `as`
    renames), `<alias>.logger` for a `loguru` / `shared.log` module alias, and
    the literal `shared.log.logger` attribute chain.
    """
    if isinstance(node, ast.Name):
        return node.id in names
    if isinstance(node, ast.Attribute) and node.attr == "logger":
        base = node.value
        if isinstance(base, ast.Name) and base.id in modules:
            return True
        return (
            isinstance(base, ast.Attribute)
            and base.attr == "log"
            and isinstance(base.value, ast.Name)
            and base.value.id == "shared"
        )
    return False


def _keyword_args(call: ast.Call) -> dict[str, ast.expr]:
    """The call's keyword arguments by name (`**spread` is not visible)."""
    return {kw.arg: kw.value for kw in call.keywords if kw.arg is not None}


def _label_only_call(
    call: ast.Call, filename: str, names: set[str], modules: set[str]
) -> LabelOnlyCall | None:
    """The finding when one call node is a label-only loguru record call.

    Chain keywords merge in write order — inner `.bind(...)` first, each later
    link next, the record call's own keywords last. loguru applies them the
    same way (verified: a later `.bind` and the record call's kwargs override
    earlier bound values), so the merged `label` and `event` are what the sink
    would resolve.
    """
    func = call.func
    if not (isinstance(func, ast.Attribute) and func.attr in _LEVEL_METHODS):
        return None

    layers = [_keyword_args(call)]  # the record call itself — outermost, wins
    node: ast.expr = func.value
    while isinstance(node, ast.Call):
        inner = node.func
        if not (isinstance(inner, ast.Attribute) and inner.attr in _CHAIN_METHODS):
            return None  # not a bind/opt chain — not this call shape
        layers.append(_keyword_args(node))
        node = inner.value
    if not _is_logger_root(node, names, modules):
        return None

    merged: dict[str, ast.expr] = {}
    for layer in reversed(layers):
        merged.update(layer)
    if "event" in merged or "label" not in merged:
        return None
    label_node = merged["label"]
    label: str | None = None
    if isinstance(label_node, ast.Constant) and isinstance(label_node.value, str):
        label = label_node.value
    return LabelOnlyCall(path=filename, lineno=call.lineno, label=label)


def find_label_only_calls(source: str, *, filename: str = "<string>") -> list[LabelOnlyCall]:
    """Every label-only loguru record call in one source string.

    "Label-only" means the merged chain keywords carry `label=` and no
    `event=`: at runtime the sink resolves event -> label -> "log", so such a
    call derives its event name from the label. Purely static; a non-literal
    label is reported with label=None (dynamic).
    """
    tree = ast.parse(source, filename=filename)
    names, modules = _logger_bindings(tree)
    findings: list[LabelOnlyCall] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            found = _label_only_call(node, filename, names, modules)
            if found is not None:
                findings.append(found)
    return sorted(findings, key=lambda f: (f.path, f.lineno))


def scan_label_only_calls(repo: Path) -> list[LabelOnlyCall]:
    """Every label-only loguru record call across production .py files."""
    findings: list[LabelOnlyCall] = []
    for rel, path in iter_production_py(repo):
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if "logger" not in source:
            continue  # a record call cannot exist without the name
        findings.extend(find_label_only_calls(source, filename=rel))
    return sorted(findings, key=lambda f: (f.path, f.lineno))


def scan_db(db_url: str) -> None:
    # Archive-only (task #1197): event_log is frozen and the agent_events
    # mirrors were removed — this path reads pre-cutover history or errors.
    import psycopg

    conn = psycopg.connect(db_url)
    cur = conn.cursor()
    print("\n===== event_log.event_type distribution (all-time) =====")
    cur.execute("SELECT event_type, COUNT(*) FROM event_log GROUP BY event_type ORDER BY 2 DESC")
    for ev, n in cur.fetchall():
        print(f"  {n:8d}  {ev}")
    print("\n===== agent_events.event by count (current month) =====")
    cur.execute(
        "SELECT event, COUNT(*) FROM agent_events "
        "WHERE ts >= date_trunc('month', now() AT TIME ZONE 'UTC') "
        "GROUP BY event ORDER BY 2 DESC"
    )
    for ev, n in cur.fetchall():
        print(f"  {n:8d}  {ev}")
    print("\n===== bare-log share (all-time) =====")
    cur.execute("SELECT COUNT(*) FROM agent_events")
    total_row = cur.fetchone()
    cur.execute("SELECT COUNT(*) FROM agent_events WHERE event = 'log'")
    bare_row = cur.fetchone()
    # COUNT(*) always returns one row with a single int column.
    total = total_row[0] if total_row is not None else 0
    bare = bare_row[0] if bare_row is not None else 0
    print(f"  total={total}  bare log={bare}  ({100.0 * bare / total:.1f}%)")
    conn.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    ap.add_argument("--db-url", help="optional prod DB URL for live distribution")
    ap.add_argument("--repo", default=str(Path.home() / "Ava"))
    args = ap.parse_args()

    event_kinds, label_kinds, event_type_kinds, sse_roles = scan_code(Path(args.repo))

    print(f"===== agent_events event= literals (prod code): {len(event_kinds)} =====")
    for k, n in event_kinds.most_common():
        print(f"  {n:4d}  {k}")
    print(f"\n===== label= fallback literals (prod code): {len(label_kinds)} =====")
    for k, n in label_kinds.most_common():
        print(f"  {n:4d}  {k}")
    print(f"\n===== event_log event_type literals (prod code): {len(event_type_kinds)} =====")
    for k, n in event_type_kinds.most_common():
        print(f"  {n:4d}  {k}")
    print(f"\n===== SSE role discriminators (shared/live_events.py): {len(sse_roles)} =====")
    for k in sorted(sse_roles):
        print(f"  {k}")

    if args.db_url:
        scan_db(args.db_url)
    return 0


if __name__ == "__main__":
    sys.exit(main())
