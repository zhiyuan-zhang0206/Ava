#!/usr/bin/env python3
"""Lint: roster tables must match their registrations.

Two tables are checked, both by set equality in both directions:

1. the runbook daemon roster (`conventions/runbook.md`) against
   `ops/spec.py:build_services()` (re-exported by `cli/commands/_repo.py`) —
   the single source of truth for the long-running sessions the cluster runs
   (gateway, restarter, labeler, milvus, memory-indexer, telegram, frontend,
   watchdog, runner, browser). The runbook carries a human-readable roster
   table documenting the same set. Nothing kept the two in sync, so a PR could
   delete (or add) a daemon and silently leave the table wrong — exactly what
   happened in #728, which removed the `scheduler` daemon but left its roster
   row + prose behind for nobody to catch.
   - a service documented in the roster but not registered in
     `build_services()` -> "deleted a daemon, forgot to undocument it" (the
     #728 case);
   - a service registered in `build_services()` but missing from the roster
     -> "added a daemon, forgot to document it".

2. the healthcheck roster (`services/healthchecks/check-roster/check-roster.ava.okf.md`)
   against the healthcheck module directory plus the ServiceSpec
   `healthcheck_module` fields and the watchdog's hand-added imports — the
   2026-08-21 audit (issue #192) found the table documenting a phantom module
   (`task_maintenance.py`) and missing seven real ones. See
   `check_healthcheck_roster()` for the exact sources of truth.

Sentinel contract: the table is located by a `<!-- lint:roster-table -->` HTML
comment placed on its own line immediately ABOVE the roster table (the one whose
header is `| Service (suffix) | Runs | Healthcheck |`). The parser then reads the
markdown rows that follow, extracting the first-column service name from its
`` `service` `` code span. Anchoring on a sentinel (not a header string or a line
number) keeps the lint robust to surrounding edits; if the sentinel is missing,
the lint errors out rather than silently passing.

Exemptions:
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
_NON_SERVICE_ROWS: set[str] = set()
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


# ── healthcheck roster (issue #192) ─────────────────────────────────────────
# The same failure class as the runbook roster, found in the 2026-08-21
# healthcheck audit: `check-roster.ava.okf.md` documented a phantom module
# (`task_maintenance.py`) and missed seven real ones. The three sources of
# truth are all structured data — the module directory, the ServiceSpec
# roster, and the watchdog's hand-added imports — so set equality pins all
# three to the doc table.

_HEALTHCHECK_SENTINEL = "<!-- lint:healthcheck-roster-table -->"

# The roster table lives in its own subdirectory (the node outgrew the
# healthchecks overview — 2026-08-30 split); the modules it lists live
# directly in `services/healthchecks/`.
_HEALTHCHECK_DIR = _REPO_ROOT / "services" / "healthchecks"
_HEALTHCHECK_ROSTER = _HEALTHCHECK_DIR / "check-roster" / "check-roster.ava.okf.md"

_WATCHDOG_DAEMON = _REPO_ROOT / "services" / "watchdog" / "daemon.py"

# The watchdog's hand-added checks (no ServiceSpec — host policy, native
# per-cluster processes, or a compose stack; see services/watchdog/daemon.py). Parsed
# from the watchdog's own `from services.healthchecks.<x> import main as`
# imports rather than hardcoded, so a hand-added check added or removed there
# must be reflected in the roster table on the next lint run.
_HAND_ADDED_IMPORT = re.compile(
    r"^from services\.healthchecks\.(\w+) import main as \w+_healthcheck$"
)


def hand_added_healthchecks() -> set[str]:
    """The healthcheck modules the watchdog imports directly, outside the
    ServiceSpec roster (brew-pin, redis-acl, pgbouncer, lgtm)."""
    names: set[str] = set()
    for line in _WATCHDOG_DAEMON.read_text(encoding="utf-8").splitlines():
        m = _HAND_ADDED_IMPORT.match(line)
        if m:
            names.add(m.group(1))
    return names


def directory_healthchecks() -> set[str]:
    """The healthcheck module files themselves — every `*.py` in
    `services/healthchecks/`, minus `__init__.py`."""
    return {p.stem for p in _HEALTHCHECK_DIR.glob("*.py") if p.name != "__init__.py"}


def spec_healthchecks() -> set[str]:
    """The healthcheck modules registered on ServiceSpec rows inside this
    directory (`services.healthchecks.<x>`, last segment).

    Plugin-registered services keep their healthchecks in their own namespace
    (`ava_builtins.plugins.ava_fleet.task_maintenance.healthcheck`) and are out
    of scope for this table — the roster documents this directory only."""
    names: set[str] = set()
    for spec in build_services():
        hm = spec.healthcheck_module
        if hm and hm.startswith("services.healthchecks."):
            names.add(hm.rsplit(".", 1)[-1])
    return names


def parse_healthcheck_roster(text: str) -> set[str]:
    """Extract the healthcheck roster table's first-column module names
    (`browser.py`, …) — sentinel-anchored like the runbook roster; the `.py`
    suffix is stripped so the set compares against module stems."""
    idx = text.find(_HEALTHCHECK_SENTINEL)
    if idx < 0:
        raise RosterSentinelMissingError(_HEALTHCHECK_SENTINEL)

    names: set[str] = set()
    started = False
    for line in text[idx + len(_HEALTHCHECK_SENTINEL) :].splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            if started:
                break
            continue
        started = True
        m = _FIRST_CODE_SPAN.match(stripped)
        if m and m.group(1).endswith(".py"):
            names.add(m.group(1)[: -len(".py")])
    return names


def check_healthcheck_roster() -> int:
    """Assert set equality between the roster table, the module directory, and
    the ServiceSpec + hand-added registrations; return 0/1 exit code."""
    try:
        text = _HEALTHCHECK_ROSTER.read_text(encoding="utf-8")
        parsed = parse_healthcheck_roster(text)
    except RosterSentinelMissingError:
        print(
            f"healthcheck roster lint failed: sentinel {_HEALTHCHECK_SENTINEL!r} not found "
            f"in {_HEALTHCHECK_ROSTER.name} — place it on its own line immediately above "
            f"the roster table.",
            file=sys.stderr,
        )
        return 1

    directory = directory_healthchecks()
    registered = spec_healthchecks() | hand_added_healthchecks()

    if parsed != directory:
        print(
            "healthcheck roster lint failed: table does not match the module directory.",
            file=sys.stderr,
        )
        extra = parsed - directory
        missing = directory - parsed
        if extra:
            print(
                f"  roster documents healthcheck modules that do not exist: {sorted(extra)}",
                file=sys.stderr,
            )
        if missing:
            print(
                f"  healthcheck modules missing from the roster: {sorted(missing)}",
                file=sys.stderr,
            )
        return 1
    if registered != directory:
        print(
            "healthcheck roster lint failed: module directory does not match the "
            "ServiceSpec + hand-added registrations.",
            file=sys.stderr,
        )
        extra = registered - directory
        missing = directory - registered
        if extra:
            print(
                f"  ServiceSpec/hand-added healthchecks with no module file: {sorted(extra)}",
                file=sys.stderr,
            )
        if missing:
            print(
                f"  healthcheck modules not registered (ServiceSpec nor hand-added): "
                f"{sorted(missing)}",
                file=sys.stderr,
            )
        return 1

    print(f"healthcheck roster lint OK: {len(parsed)} modules match.")
    return 0


def main() -> int:
    rc = check()
    rc2 = check_healthcheck_roster()
    return rc or rc2


if __name__ == "__main__":
    sys.exit(main())
