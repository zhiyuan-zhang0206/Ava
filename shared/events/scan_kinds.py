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
import os
import re
import sys
from collections import Counter
from collections.abc import Iterator
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


def walk_py(repo: Path) -> Iterator[tuple[str, list[str]]]:
    """Yield (relative_path, lines) for every production .py file."""
    for dirpath, dirnames, filenames in os.walk(repo):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            path = Path(dirpath) / fn
            rel = str(path.relative_to(repo))
            if "/tests/" in rel or rel.startswith("tests/"):
                continue
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
    print("\n===== agent_events.event top 40 (current month) =====")
    cur.execute(
        "SELECT event, COUNT(*) FROM agent_events "
        "WHERE ts >= date_trunc('month', now() AT TIME ZONE 'UTC') "
        "GROUP BY event ORDER BY 2 DESC LIMIT 40"
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
