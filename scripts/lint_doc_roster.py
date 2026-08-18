#!/usr/bin/env python3
"""Lint: the runbook daemon roster table must match the registered services.

`ops/spec.py:build_services()` (re-exported by `cli/commands/_repo.py`) is the
single source of truth for the
long-running sessions the cluster runs (gateway, restarter, labeler, milvus,
memory-indexer, telegram, frontend, watchdog, runner, browser). The runbook
(`conventions/runbook.md`) carries a human-readable roster table documenting the
same set. Nothing keeps the two in sync, so a PR can delete (or add) a daemon and
silently leave the table wrong — exactly what happened in #728, which removed the
`scheduler` daemon but left its roster row + prose behind for nobody to catch.

This lint closes that gap. It asserts SET EQUALITY in both directions:
  - a service documented in the roster but not registered in `build_services()`
    -> "deleted a daemon, forgot to undocument it" (the #728 case);
  - a service registered in `build_services()` but missing from the roster
    -> "added a daemon, forgot to document it".

Sentinel contract: the table is located by a `<!-- lint:roster-table -->` HTML
comment placed on its own line immediately ABOVE the roster table (the one whose
header is `| Service (suffix) | Runs | Healthcheck |`). The parser then reads the
markdown rows that follow, extracting the first-column service name from its
`` `service` `` code span. Anchoring on a sentinel (not a header string or a line
number) keeps the lint robust to surrounding edits; if the sentinel is missing,
the lint errors out rather than silently passing.

Exemptions:
  - `_NON_SERVICE_ROWS` — rows that legitimately appear in the table but are not
    `build_services()` entries (the per-agent `agent-{N}` row).
  - `_ROSTER_EXEMPT` — escape valve (normally empty), matching the allowlist
    convention of the other local lints.

Zero-FP basis: both sides are structured data — a Python tuple and a markdown
table column — never prose. The only fragility (table location) is killed by the
sentinel. Prose `scheduler`-style mentions elsewhere in the docs are out of scope
here (manual fix + the sweeper `docs-aging` class); this lint guards the
structured table only.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from cli.commands._repo import build_services

_REPO_ROOT = Path(__file__).resolve().parent.parent
_RUNBOOK = _REPO_ROOT / "conventions" / "runbook.md"

_SENTINEL = "<!-- lint:roster-table -->"

# Rows that appear in the roster table but are not build_services() entries.
_NON_SERVICE_ROWS = {"agent-{N}"}
# Escape valve (normally empty); same allowlist convention as the other lints.
_ROSTER_EXEMPT: set[str] = set()

# First-column code span of a markdown table row: `| `service` | ... |`.
_FIRST_CODE_SPAN = re.compile(r"^\|\s*`([^`]+)`")


class RosterSentinelMissingError(Exception):
    """The `<!-- lint:roster-table -->` sentinel was not found in the runbook."""


def parse_roster(text: str) -> set[str]:
    """Extract the roster table's first-column service names from `text`.

    Locates the sentinel, skips to the table that follows, and reads the
    first-column `` `service` `` code span of each table row until the table
    ends (first line that is not a `|`-row). The header / separator rows have no
    leading code span and are skipped naturally.

    Raises RosterSentinelMissingError if the sentinel is absent.
    """
    idx = text.find(_SENTINEL)
    if idx < 0:
        raise RosterSentinelMissingError(_SENTINEL)

    names: set[str] = set()
    started = False
    for line in text[idx + len(_SENTINEL) :].splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            if started:
                # Table ended (blank line / prose after the rows).
                break
            # Still between the sentinel and the table header.
            continue
        started = True
        m = _FIRST_CODE_SPAN.match(stripped)
        if m:
            names.add(m.group(1))
    return names


def check() -> int:
    """Compare the parsed roster against build_services(); return 0/1 exit code."""
    registered = {spec.session for spec in build_services()}

    text = _RUNBOOK.read_text(encoding="utf-8")
    try:
        parsed = parse_roster(text)
    except RosterSentinelMissingError:
        print(
            f"roster lint failed: sentinel {_SENTINEL!r} not found in "
            f"{_RUNBOOK.name} — place it on its own line "
            f"immediately above the roster table.",
            file=sys.stderr,
        )
        return 1

    documented = parsed - _NON_SERVICE_ROWS - _ROSTER_EXEMPT

    extra = documented - registered
    missing = registered - documented
    if extra or missing:
        print(
            "roster lint failed: runbook roster does not match build_services().", file=sys.stderr
        )
        if extra:
            print(
                f"  roster documents services not registered: {sorted(extra)}",
                file=sys.stderr,
            )
        if missing:
            print(
                f"  registered services missing from roster: {sorted(missing)}",
                file=sys.stderr,
            )
        return 1

    print(f"roster lint OK: {len(documented)} services match build_services().")
    return 0


def main() -> int:
    return check()


if __name__ == "__main__":
    sys.exit(main())
