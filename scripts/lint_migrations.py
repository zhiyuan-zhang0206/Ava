"""Lint the `migrations/` directory for the timestamp-id + applied-set scheme.

CI runs it (`.venv/bin/python scripts/lint_migrations.py`); fail -> exit 1. Also
fine to run locally once after adding a new migration.

Checks:
1. **Filename format** — every up-migration matches
   `YYYYMMDDTHHMMSS_<kebab-name>.sql` and every down matches the `.down.sql`
   variant (the regexes are reused from `shared.migrations`, so lint and the
   runtime loader can never disagree on the format). The timestamp part must
   parse as a real UTC datetime (catches fat-fingered `20261301T...`).
2. **Unique names** — no two files share a name (the applied-set primary key
   would reject a duplicate at apply; catch it here).
3. **Unique timestamp prefixes** — no two migrations share the same
   `YYYYMMDDTHHMMSS` prefix. Names stay second-precision by ruling (2026-08-07),
   so a shared prefix means two distinct migrations are timestamp-ambiguous;
   rollback diffs and operator chatter key off these names, and a pair that
   differs only in the kebab tail is one typo away from colliding.
4. **up/down pairing** — every `*.sql` has a matching `*.down.sql` and vice
   versa. The baseline is the rollback floor, so every post-baseline migration
   must be reversible.
5. **schema.sql baseline seed** — `db/schema.sql` must stamp the baseline
   sentinel row and must NOT still carry the pre-cutover `generate_series(...)`
   seed (the applied-set bootstrap replaced it).
6. **down IF EXISTS symmetry** — every top-level DROP in a `.down.sql` must
   carry `IF EXISTS`, so a repeated or standalone rollback cannot blow up on a
   schema that already lacks the object (drops inside guarded DO blocks are
   exempt).

Deliberately **no** continuity / next-number / cross-branch-collision checks:
timestamp names are collision-free by construction, which is the whole point of
the 2026-07-19 re-baseline (the 0060 / 0062 / 0080 numbering collisions).
"""

from __future__ import annotations

import re
import sys
from datetime import datetime
from pathlib import Path

from shared.migrations import _BASELINE_NAME, _DOWN_FILENAME_RE, _FILENAME_RE

REPO_ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS_DIR = REPO_ROOT / "migrations"
SCHEMA_SQL = REPO_ROOT / "db" / "schema.sql"

_FORMAT_HINT = "expected YYYYMMDDTHHMMSS_<kebab-name>.sql"


def _timestamp_valid(stem: str) -> bool:
    """The `YYYYMMDDTHHMMSS` prefix of a migration stem parses as a real datetime."""
    ts = stem.split("_", 1)[0]
    try:
        datetime.strptime(ts, "%Y%m%dT%H%M%S")  # noqa: DTZ007 — naming convention, not a real instant
    except ValueError:
        return False
    return True


def _collect_migrations() -> tuple[set[str], set[str], list[str]]:
    """Scan migrations/; return (up stems, down stems, error list)."""
    errors: list[str] = []
    ups: set[str] = set()
    downs: set[str] = set()
    prefixes: set[str] = set()
    if not MIGRATIONS_DIR.is_dir():
        errors.append(f"migrations/ directory does not exist: {MIGRATIONS_DIR}")
        return ups, downs, errors

    for entry in sorted(MIGRATIONS_DIR.iterdir()):
        if entry.is_dir():
            errors.append(f"migrations/ should not have subdirectories: {entry.name}")
            continue
        if entry.name.startswith(".") or entry.name == "README.md":
            continue
        if entry.name.endswith(".down.sql"):
            dm = _DOWN_FILENAME_RE.match(entry.name)
            if not dm:
                errors.append(
                    f"non-conforming down name: {entry.name} ({_FORMAT_HINT[:-4]}.down.sql)"
                )
            else:
                downs.add(dm.group(1))
            continue
        if not entry.name.endswith(".sql"):
            errors.append(f"non-.sql file: {entry.name}")
            continue
        m = _FILENAME_RE.match(entry.name)
        if not m:
            errors.append(f"non-conforming name: {entry.name} ({_FORMAT_HINT})")
            continue
        stem = m.group(1)
        if stem in ups:
            errors.append(f"duplicate migration name: {stem!r}")
        prefix = stem.split("_", 1)[0]
        if prefix in prefixes:
            errors.append(
                f"{entry.name}: duplicate timestamp prefix {prefix!r} — two "
                "migrations share it; pick a distinct second for each"
            )
        prefixes.add(prefix)
        if not _timestamp_valid(stem):
            errors.append(f"{entry.name}: timestamp prefix is not a valid datetime")
        if stem == _BASELINE_NAME:
            errors.append(
                f"{entry.name}: name collides with the reserved baseline sentinel "
                f"{_BASELINE_NAME!r} (the baseline is a schema.sql row, never a file)"
            )
        ups.add(stem)

    return ups, downs, errors


def _check_pairing(ups: set[str], downs: set[str]) -> list[str]:
    """Every up needs a matching down and vice versa."""
    errors: list[str] = []
    for stem in sorted(ups - downs):
        errors.append(
            f"{stem}.sql has no matching {stem}.down.sql (post-baseline migrations must be reversible)"
        )
    for stem in sorted(downs - ups):
        errors.append(f"{stem}.down.sql has no matching up migration {stem}.sql")
    return errors


def _check_schema_seed() -> list[str]:
    """db/schema.sql must stamp the baseline sentinel and must not carry the
    pre-cutover generate_series seed."""
    if not SCHEMA_SQL.is_file():
        return [f"db/schema.sql does not exist: {SCHEMA_SQL}"]
    text = SCHEMA_SQL.read_text(encoding="utf-8")
    errors: list[str] = []
    if "generate_series" in text:
        errors.append(
            "db/schema.sql still contains a `generate_series(...)` seed — the "
            "applied-set bootstrap stamps a single baseline row instead; remove it"
        )
    if _BASELINE_NAME not in text:
        errors.append(
            f"db/schema.sql does not stamp the baseline sentinel {_BASELINE_NAME!r} "
            "into schema_migrations — a fresh DB would then look un-baselined and "
            "shared.migrations would treat everything as pending"
        )
    return errors


def _check_down_if_exists() -> list[str]:
    """Every top-level DROP in a .down.sql must carry IF EXISTS.

    A down must be re-runnable: a rollback that fails partway is retried, and a
    standalone down of one migration is a supported recovery shape — both blow
    up on a schema that already lacks the dropped object. Drops inside DO
    blocks are deliberately NOT checked: those are guarded by the block's own
    EXISTS/relkind checks (e.g. the monthly-partitioning down). Heuristic by
    design — a lint tripwire for the common trap, not a SQL parser. (audit P2,
    Fable backend-shared: four downs shipped bare DROPs, and 20260805T083741's
    down referenced the pre-rename `kind` column, blowing up standalone.)"""
    errors: list[str] = []
    for entry in sorted(MIGRATIONS_DIR.iterdir()):
        if not entry.name.endswith(".down.sql"):
            continue
        for lineno, line in enumerate(entry.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if not stripped or stripped.startswith("--"):
                continue
            if line[:1].isspace():
                continue  # inside a DO block or plpgsql body
            m = re.match(
                r"^DROP\s+((?:MATERIALIZED\s+)?VIEW|TABLE|COLUMN|INDEX|SEQUENCE|TYPE|SCHEMA|FUNCTION|TRIGGER)\b",
                stripped,
                re.IGNORECASE,
            )
            if m and "IF EXISTS" not in stripped.upper():
                errors.append(
                    f"{entry.name}:{lineno}: top-level DROP {m.group(1)} without "
                    "IF EXISTS — a repeated / standalone rollback fails; add "
                    "IF EXISTS or move the drop into a guarded DO block"
                )
    return errors


def main() -> int:
    ups, downs, errors = _collect_migrations()
    errors.extend(_check_pairing(ups, downs))
    errors.extend(_check_schema_seed())
    errors.extend(_check_down_if_exists())

    if errors:
        print("migration lint failed:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1

    print(f"migration lint OK: {len(ups)} post-baseline migration(s), baseline seed aligned")
    return 0


if __name__ == "__main__":
    sys.exit(main())
