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
   seed. It may additionally stamp migration names whose non-idempotent changes
   are already folded into the current schema.
6. **down IF EXISTS symmetry** — every top-level DROP in a `.down.sql` must
   carry `IF EXISTS`, so a repeated or standalone rollback cannot blow up on a
   schema that already lacks the object (drops inside guarded DO blocks are
   exempt).
7. **rollback-snapshot retirement** — every table following the shared
   `*_backfill_*` rollback-snapshot convention that is created by an up
   migration must have a later up migration that drops it. The archive CLI
   accepts the same convention; the table is a finite recovery buffer, never
   durable application state.

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
from shared.rollback_snapshot import is_rollback_snapshot_table

REPO_ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS_DIR = REPO_ROOT / "migrations"
SCHEMA_SQL = REPO_ROOT / "db" / "schema.sql"

_FORMAT_HINT = "expected YYYYMMDDTHHMMSS_<kebab-name>.sql"
_CREATE_TABLE_RE = re.compile(
    r"\bCREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(?P<table>[a-z_][a-z0-9_]*)\b",
    re.IGNORECASE,
)
_DROP_TABLE_IF_EXISTS_RE = re.compile(
    r"\bDROP\s+TABLE\s+IF\s+EXISTS\s+(?:[a-z_][a-z0-9_]*\.)?(?P<table>[a-z_][a-z0-9_]*)\b",
    re.IGNORECASE,
)
_DOLLAR_QUOTE_TAG_RE = re.compile(r"(?:[A-Za-z_][A-Za-z0-9_]*)?$")
_DO_PREFIX_RE = re.compile(r"\s*DO(?:\s+LANGUAGE\s+[A-Za-z_][A-Za-z0-9_]*)?\s*$", re.IGNORECASE)


def _masked_text(fragment: str) -> str:
    """Replace a non-static SQL fragment while preserving its line layout."""
    return "".join("\n" if char == "\n" else " " for char in fragment)


def _mask_single_quoted_literal(text: str, start: int) -> tuple[str, int]:
    """Return a masked ordinary SQL literal and the position after it."""
    position = start + 1
    while position < len(text):
        if text[position] == "\\" and position + 1 < len(text):
            position += 2
            continue
        if text[position] == "'":
            if text.startswith("''", position):
                position += 2
                continue
            position += 1
            break
        position += 1
    return _masked_text(text[start:position]), position


def _dollar_quoted_body(text: str, start: int) -> tuple[str, str, int] | None:
    """Return a dollar-quoted delimiter, body, and end position when present."""
    delimiter_end = text.find("$", start + 1)
    if delimiter_end == -1:
        return None
    delimiter = text[start : delimiter_end + 1]
    if not _DOLLAR_QUOTE_TAG_RE.fullmatch(delimiter[1:-1]):
        return None
    body_start = delimiter_end + 1
    body_end = text.find(delimiter, body_start)
    if body_end == -1:
        return None
    return delimiter, text[body_start:body_end], body_end + len(delimiter)


def _mask_nonstatic_sql(text: str) -> str:
    """Mask comments and literals while retaining static DDL in anonymous DO blocks.

    Other dollar-quoted strings, ordinary strings, and comments are masked to
    prevent descriptive text or dynamic SQL from satisfying the snapshot-
    retirement convention. This remains a narrow convention check, not a
    general SQL parser.
    """
    masked: list[str] = []
    position = 0

    while position < len(text):
        if text.startswith("--", position):
            end = text.find("\n", position)
            if end == -1:
                masked.append(_masked_text(text[position:]))
                break
            masked.append(_masked_text(text[position:end]))
            masked.append("\n")
            position = end + 1
            continue

        if text.startswith("/*", position):
            end = text.find("*/", position + 2)
            if end == -1:
                masked.append(_masked_text(text[position:]))
                break
            end += 2
            masked.append(_masked_text(text[position:end]))
            position = end
            continue

        if text[position] == "'":
            literal, position = _mask_single_quoted_literal(text, position)
            masked.append(literal)
            continue

        if text[position] == "$":
            dollar_quote = _dollar_quoted_body(text, position)
            if dollar_quote is not None:
                delimiter, body, position = dollar_quote
                prefix = "".join(masked).rsplit(";", 1)[-1]
                masked.append(_masked_text(delimiter))
                masked.append(
                    _mask_nonstatic_sql(body)
                    if _DO_PREFIX_RE.fullmatch(prefix)
                    else _masked_text(body)
                )
                masked.append(_masked_text(delimiter))
                continue

        masked.append(text[position])
        position += 1

    return "".join(masked)


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


def _check_backfill_snapshot_drop_plans() -> list[str]:
    """Require every temporary `*_backfill_*` table to have a later drop migration.

    The check intentionally reads only up migrations: a down migration removes a
    snapshot when rolling a correction back, but is not the forward retirement
    plan that reclaims it once recovery data is no longer needed. This is a
    naming convention, not a SQL parser; migration table names are unquoted
    lowercase identifiers by repository convention.
    """
    creations: list[tuple[str, str]] = []
    drops: dict[str, list[str]] = {}

    for entry in sorted(MIGRATIONS_DIR.iterdir()):
        if not entry.name.endswith(".sql") or entry.name.endswith(".down.sql"):
            continue
        text = _mask_nonstatic_sql(entry.read_text(encoding="utf-8"))
        for match in _CREATE_TABLE_RE.finditer(text):
            table = match.group("table").lower()
            if is_rollback_snapshot_table(table):
                creations.append((entry.name, table))
        for match in _DROP_TABLE_IF_EXISTS_RE.finditer(text):
            table = match.group("table").lower()
            drops.setdefault(table, []).append(entry.name)

    errors: list[str] = []
    for created_by, table in creations:
        if any(dropped_by > created_by for dropped_by in drops.get(table, [])):
            continue
        errors.append(
            f"{created_by}: rollback snapshot table {table!r} has no later drop plan — "
            "add a later up migration with DROP TABLE IF EXISTS after its recovery data is archived"
        )
    return errors


def main() -> int:
    ups, downs, errors = _collect_migrations()
    errors.extend(_check_pairing(ups, downs))
    errors.extend(_check_schema_seed())
    errors.extend(_check_down_if_exists())
    errors.extend(_check_backfill_snapshot_drop_plans())

    if errors:
        print("migration lint failed:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1

    print(f"migration lint OK: {len(ups)} post-baseline migration(s), baseline seed aligned")
    return 0


if __name__ == "__main__":
    sys.exit(main())
