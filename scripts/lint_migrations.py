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
   seed. It must also stamp every migration name whose non-idempotent change is
   already folded into the current schema — the stamp is what keeps a fresh DB
   from replaying that change. Check 8 enforces the mechanically detectable
   subset of this rule, where a fresh-DB replay would definitely fail; the
   replay-safe shapes (idempotent DDL, guarded DO blocks, in-place rebuilds)
   stay exempt by design.
6. **down IF EXISTS symmetry** — every top-level DROP in a `.down.sql` must
   carry `IF EXISTS`, so a repeated or standalone rollback cannot blow up on a
   schema that already lacks the object (drops inside guarded DO blocks are
   exempt).
7. **rollback-snapshot retirement** — every table following the shared
   `*_backfill_*` rollback-snapshot convention that is created by an up
   migration must have a later up migration that drops it. The archive CLI
   accepts the same convention; the table is a finite recovery buffer, never
   durable application state.
8. **folded strict migrations must be seeded** — an unseeded up migration must
   not carry a strict (non-idempotent) DDL statement whose object already
   exists in `db/schema.sql`: a fresh DB replays every unseeded migration over
   the baseline (the migration smoke builds exactly that DB), and the statement
   would die on "already exists". Covers `ADD COLUMN`, `CREATE TABLE`,
   `CREATE INDEX`, `ADD CONSTRAINT`, and `CREATE TRIGGER`, matched
   table-qualified so a same-named object on another table never trips it.
   Exempt by design: statements the same or an earlier unseeded migration drops
   first (a replay rebuild), statements inside an anonymous `DO` block (the
   block owns its guard), and objects absent from the baseline (they cannot hit
   already-exists — whether the baseline is missing the change at all is the
   smoke convergence gate's question). Heuristic by design — a tripwire for the
   convention, not a SQL parser.

Deliberately **no** continuity / next-number / cross-branch-collision checks:
timestamp names are collision-free by construction, which is the whole point of
the 2026-07-19 re-baseline (the 0060 / 0062 / 0080 numbering collisions).
"""

from __future__ import annotations

import re
import sys
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

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

# Check 8: strict per-object DDL patterns, matched table-qualified against
# db/schema.sql. "Strict" = no IF NOT EXISTS / OR REPLACE: replaying it over a
# schema that already holds the object fails.
_DdlCandidate = tuple[str, str | None, str, bool, int]  # (kind, table, name, idempotent, position)
_DropKeys = dict[str, set[tuple[str | None, str]]]  # kind -> {(table, name)}
_MIGRATION_SEED_RE = re.compile(
    r"INSERT\s+INTO\s+schema_migrations\s*\(\s*name\s*\)\s*VALUES\s*\(\s*'(?P<name>[^']+)'\s*\)",
    re.IGNORECASE,
)
_ALTER_TABLE_STMT_RE = re.compile(
    r"\bALTER\s+TABLE\s+(?:ONLY\s+)?(?:[a-z_][a-z0-9_]*\.)?(?P<table>[a-z_][a-z0-9_]*)(?P<body>[^;]*)",
    re.IGNORECASE,
)
_ADD_COLUMN_RE = re.compile(
    r"\bADD\s+COLUMN\s+(?P<guard>IF\s+NOT\s+EXISTS\s+)?(?P<column>[a-z_][a-z0-9_]*)",
    re.IGNORECASE,
)
_DROP_COLUMN_RE = re.compile(
    r"\bDROP\s+COLUMN\s+(?:IF\s+EXISTS\s+)?(?P<column>[a-z_][a-z0-9_]*)",
    re.IGNORECASE,
)
_CREATE_TABLE_STMT_RE = re.compile(
    r"\bCREATE\s+TABLE\s+(?P<guard>IF\s+NOT\s+EXISTS\s+)?(?:[a-z_][a-z0-9_]*\.)?(?P<table>[a-z_][a-z0-9_]*)",
    re.IGNORECASE,
)
_DROP_TABLE_RE = re.compile(
    r"\bDROP\s+TABLE\s+(?:IF\s+EXISTS\s+)?(?:[a-z_][a-z0-9_]*\.)?(?P<table>[a-z_][a-z0-9_]*)",
    re.IGNORECASE,
)
_CREATE_INDEX_STMT_RE = re.compile(
    r"\bCREATE\s+(?:UNIQUE\s+)?INDEX\s+(?:CONCURRENTLY\s+)?(?P<guard>IF\s+NOT\s+EXISTS\s+)?"
    r"(?P<index>[a-z_][a-z0-9_]*)\b[^;]*?\bON\s+(?:ONLY\s+)?(?:[a-z_][a-z0-9_]*\.)?(?P<table>[a-z_][a-z0-9_]*)",
    re.IGNORECASE,
)
_DROP_INDEX_RE = re.compile(
    r"\bDROP\s+INDEX\s+(?:IF\s+EXISTS\s+)?(?:[a-z_][a-z0-9_]*\.)?(?P<index>[a-z_][a-z0-9_]*)",
    re.IGNORECASE,
)
_ADD_CONSTRAINT_RE = re.compile(
    r"\bADD\s+CONSTRAINT\s+(?P<constraint>[a-z_][a-z0-9_]*)", re.IGNORECASE
)
_DROP_CONSTRAINT_RE = re.compile(
    r"\bDROP\s+CONSTRAINT\s+(?:IF\s+EXISTS\s+)?(?P<constraint>[a-z_][a-z0-9_]*)",
    re.IGNORECASE,
)
_CREATE_TRIGGER_STMT_RE = re.compile(
    r"\b(?P<create>CREATE\s+(?:OR\s+REPLACE\s+)?(?:CONSTRAINT\s+)?TRIGGER)\s+"
    r"(?P<trigger>[a-z_][a-z0-9_]*)\b[^;]*?\bON\s+(?:ONLY\s+)?(?:[a-z_][a-z0-9_]*\.)?(?P<table>[a-z_][a-z0-9_]*)",
    re.IGNORECASE,
)
_DROP_TRIGGER_RE = re.compile(
    r"\bDROP\s+TRIGGER\s+(?:IF\s+EXISTS\s+)?(?P<trigger>[a-z_][a-z0-9_]*)\s+ON\s+"
    r"(?:ONLY\s+)?(?:[a-z_][a-z0-9_]*\.)?(?P<table>[a-z_][a-z0-9_]*)",
    re.IGNORECASE,
)
_NON_COLUMN_CLAUSE_WORDS = frozenset(
    {"primary", "unique", "check", "foreign", "exclude", "like", "constraint"}
)


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


def _mask_nonstatic_sql(text: str) -> tuple[str, list[tuple[int, int]]]:
    """Mask comments and literals while retaining static DDL in anonymous DO blocks.

    Other dollar-quoted strings, ordinary strings, and comments are masked to
    prevent descriptive text or dynamic SQL from satisfying the snapshot-
    retirement convention. This remains a narrow convention check, not a
    general SQL parser.

    Returns the masked text plus the spans (in original coordinates — every
    masked fragment keeps its length and line layout) of the anonymous DO
    blocks whose bodies stay unmasked, so callers can exempt the statements
    inside them.
    """
    masked: list[str] = []
    do_spans: list[tuple[int, int]] = []
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
                delimiter, body, end = dollar_quote
                prefix = "".join(masked).rsplit(";", 1)[-1]
                masked.append(_masked_text(delimiter))
                if _DO_PREFIX_RE.fullmatch(prefix):
                    body_masked, nested_spans = _mask_nonstatic_sql(body)
                    offset = position + len(delimiter)
                    do_spans.append((position, end))
                    do_spans.extend(
                        (nested_start + offset, nested_stop + offset)
                        for nested_start, nested_stop in nested_spans
                    )
                    masked.append(body_masked)
                else:
                    masked.append(_masked_text(body))
                masked.append(_masked_text(delimiter))
                position = end
                continue

        masked.append(text[position])
        position += 1

    return "".join(masked), do_spans


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
    pre-cutover generate_series seed.

    Seed *completeness* — stamp every migration whose non-idempotent change is
    folded in — is enforced by check 8 for its mechanically detectable subset;
    this function guards only the sentinel and the retired cutover seed."""
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
        text, _ = _mask_nonstatic_sql(entry.read_text(encoding="utf-8"))
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


def _migration_seed_names(schema_text: str) -> set[str]:
    """Migration names stamped by db/schema.sql's baseline seed section.

    Read from the raw text, skipping comment lines — the masked text would have
    the quoted names blanked. A name read here that has since departed from
    migrations/ is harmless: it only ever exempts a name from the check.
    """
    without_comments = "\n".join(
        "" if line.lstrip().startswith("--") else line for line in schema_text.splitlines()
    )
    return {match.group("name") for match in _MIGRATION_SEED_RE.finditer(without_comments)}


class _SchemaCatalog(NamedTuple):
    """Objects db/schema.sql creates — what a fresh DB holds before replaying
    any post-baseline migration — each keyed by the table that owns it."""

    tables: set[str]
    columns: dict[str, set[str]]
    indexes: dict[str, set[str]]
    constraints: dict[str, set[str]]
    triggers: dict[str, set[str]]


def _balanced_table_body(text: str, start: int) -> str | None:
    """The parenthesized CREATE TABLE body whose `(` follows `start`.

    None when the next `(` is not the body opener (e.g. a `CREATE TABLE ... AS
    SELECT`). The masked text has every string blanked, so between the table
    name and its body only whitespace can sit.
    """
    open_paren = text.find("(", start)
    if open_paren == -1 or text[start:open_paren].strip():
        return None
    depth = 0
    for position in range(open_paren, len(text)):
        char = text[position]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return text[open_paren + 1 : position]
    return None


def _table_body_objects(body: str) -> tuple[set[str], set[str]]:
    """(column names, inline constraint names) from a CREATE TABLE body.

    Only depth-1 rows are read, so a CHECK / PRIMARY KEY expression
    continuation cannot leak its identifiers in as column names.
    """
    columns: set[str] = set()
    constraints: set[str] = set()
    depth = 1
    for line in body.splitlines():
        at_top_level = depth == 1
        for char in line:
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
        if not at_top_level:
            continue
        stripped = line.strip()
        match = re.match(r"([a-z_][a-z0-9_]*)\s", stripped, re.IGNORECASE)
        if not match:
            continue
        head = match.group(1).lower()
        if head == "constraint":
            constraint = re.match(r"constraint\s+([a-z_][a-z0-9_]*)", stripped, re.IGNORECASE)
            if constraint:
                constraints.add(constraint.group(1).lower())
            continue
        if head in _NON_COLUMN_CLAUSE_WORDS:
            continue
        columns.add(head)
    return columns, constraints


def _schema_catalog(schema_text: str) -> _SchemaCatalog:
    """Extract the objects db/schema.sql creates, from its masked text."""
    tables: set[str] = set()
    columns: dict[str, set[str]] = {}
    indexes: dict[str, set[str]] = {}
    constraints: dict[str, set[str]] = {}
    triggers: dict[str, set[str]] = {}

    for match in _CREATE_TABLE_STMT_RE.finditer(schema_text):
        table = match.group("table").lower()
        tables.add(table)
        body = _balanced_table_body(schema_text, match.end())
        if body is None:
            continue
        body_columns, body_constraints = _table_body_objects(body)
        columns.setdefault(table, set()).update(body_columns)
        constraints.setdefault(table, set()).update(body_constraints)

    for match in _ALTER_TABLE_STMT_RE.finditer(schema_text):
        table = match.group("table").lower()
        body = match.group("body")
        columns.setdefault(table, set()).update(
            add.group("column").lower() for add in _ADD_COLUMN_RE.finditer(body)
        )
        constraints.setdefault(table, set()).update(
            add.group("constraint").lower() for add in _ADD_CONSTRAINT_RE.finditer(body)
        )

    for match in _CREATE_INDEX_STMT_RE.finditer(schema_text):
        indexes.setdefault(match.group("table").lower(), set()).add(match.group("index").lower())
    for match in _CREATE_TRIGGER_STMT_RE.finditer(schema_text):
        triggers.setdefault(match.group("table").lower(), set()).add(match.group("trigger").lower())

    return _SchemaCatalog(tables, columns, indexes, constraints, triggers)


def _ddl_usage(masked_text: str) -> tuple[list[_DdlCandidate], _DropKeys]:
    """Strict-DDL candidates and drop usage from an already-masked migration text.

    Candidates are one per statement that creates/extends an object — including
    the idempotent forms, flagged as such; drops are per-kind `(table, name)`
    key sets (a table or index drop keys by name alone: it needs no binder).
    """
    adds: list[_DdlCandidate] = []
    drops: _DropKeys = {
        "column": set(),
        "table": set(),
        "index": set(),
        "constraint": set(),
        "trigger": set(),
    }

    for match in _ALTER_TABLE_STMT_RE.finditer(masked_text):
        table = match.group("table").lower()
        body = match.group("body")
        body_start = match.start("body")
        for add in _ADD_COLUMN_RE.finditer(body):
            adds.append(
                (
                    "column",
                    table,
                    add.group("column").lower(),
                    bool(add.group("guard")),
                    body_start + add.start(),
                )
            )
        for add in _ADD_CONSTRAINT_RE.finditer(body):
            adds.append(
                (
                    "constraint",
                    table,
                    add.group("constraint").lower(),
                    False,
                    body_start + add.start(),
                )
            )
        for drop in _DROP_COLUMN_RE.finditer(body):
            drops["column"].add((table, drop.group("column").lower()))
        for drop in _DROP_CONSTRAINT_RE.finditer(body):
            drops["constraint"].add((table, drop.group("constraint").lower()))

    for match in _CREATE_TABLE_STMT_RE.finditer(masked_text):
        adds.append(
            ("table", None, match.group("table").lower(), bool(match.group("guard")), match.start())
        )
    for match in _DROP_TABLE_RE.finditer(masked_text):
        drops["table"].add((None, match.group("table").lower()))
    for match in _CREATE_INDEX_STMT_RE.finditer(masked_text):
        adds.append(
            (
                "index",
                match.group("table").lower(),
                match.group("index").lower(),
                bool(match.group("guard")),
                match.start(),
            )
        )
    for match in _DROP_INDEX_RE.finditer(masked_text):
        drops["index"].add((None, match.group("index").lower()))
    for match in _CREATE_TRIGGER_STMT_RE.finditer(masked_text):
        adds.append(
            (
                "trigger",
                match.group("table").lower(),
                match.group("trigger").lower(),
                "or replace" in match.group("create").lower(),
                match.start(),
            )
        )
    for match in _DROP_TRIGGER_RE.finditer(masked_text):
        drops["trigger"].add((match.group("table").lower(), match.group("trigger").lower()))

    return adds, drops


def _is_folded(catalog: _SchemaCatalog, kind: str, table: str | None, name: str) -> bool:
    """Whether db/schema.sql already holds the object this statement targets."""
    if kind == "table":
        return name in catalog.tables
    if table is None:
        return False
    if kind == "column":
        return name in catalog.columns.get(table, set())
    if kind == "index":
        return name in catalog.indexes.get(table, set())
    if kind == "constraint":
        return name in catalog.constraints.get(table, set())
    if kind == "trigger":
        return name in catalog.triggers.get(table, set())
    raise AssertionError(f"unknown strict-DDL kind: {kind!r}")


def _dropped_for_replay(drops: _DropKeys, kind: str, table: str | None, name: str) -> bool:
    """Whether any unseeded migration drops this object first — a replay
    rebuild (the replay removes it before this statement re-creates it)."""
    keys = drops[kind]
    return (table, name) in keys or (None, name) in keys


def _check_folded_strict_without_seed() -> list[str]:
    """An unseeded migration must not carry a strict (non-idempotent) DDL
    statement whose object already exists in db/schema.sql.

    A fresh DB stamps the baseline seed and then replays every migration file
    the seed does not stamp — scripts/test_migrations_apply.sh builds exactly
    that DB — so such a statement fails on "already exists". This check is the
    static front of that smoke: the mechanically provable "a replay would
    definitely fail" subset, matched table-qualified (a same-named object on
    another table never trips it).

    Deliberately not checked: strict DROPs (their samples are rebuild halves
    and replay chains — not tractably provable statically), objects absent from
    db/schema.sql (a replay cannot hit already-exists), and rare strict kinds
    like CREATE TYPE (the smoke covers those).
    """
    if not SCHEMA_SQL.is_file():
        return [f"db/schema.sql does not exist: {SCHEMA_SQL}"]
    raw_schema = SCHEMA_SQL.read_text(encoding="utf-8")
    seeded = _migration_seed_names(raw_schema)
    masked_schema, _ = _mask_nonstatic_sql(raw_schema)
    catalog = _schema_catalog(masked_schema)

    unseeded: list[tuple[str, Path]] = []
    for entry in sorted(MIGRATIONS_DIR.iterdir()):
        if not entry.name.endswith(".sql") or entry.name.endswith(".down.sql"):
            continue
        match = _FILENAME_RE.match(entry.name)
        if match and match.group(1) not in seeded:
            unseeded.append((match.group(1), entry))

    # Collect every unseeded migration's drops first: an object another
    # unseeded migration drops is replay-safe here, wherever the file sits.
    facts: list[tuple[str, list[_DdlCandidate], list[tuple[int, int]]]] = []
    all_drops: _DropKeys = {
        "column": set(),
        "table": set(),
        "index": set(),
        "constraint": set(),
        "trigger": set(),
    }
    for stem, path in unseeded:
        masked, do_spans = _mask_nonstatic_sql(path.read_text(encoding="utf-8"))
        adds, drops = _ddl_usage(masked)
        facts.append((stem, adds, do_spans))
        for kind, keys in drops.items():
            all_drops[kind] |= keys

    errors: list[str] = []
    for stem, adds, do_spans in facts:
        for kind, table, name, idempotent, position in adds:
            if idempotent:
                continue
            if any(start <= position < stop for start, stop in do_spans):
                continue  # inside a DO block — the block owns its guard
            if not _is_folded(catalog, kind, table, name):
                continue
            if _dropped_for_replay(all_drops, kind, table, name):
                continue
            statement = {
                "column": f"ALTER TABLE {table} ADD COLUMN {name}",
                "table": f"CREATE TABLE {name}",
                "index": f"CREATE INDEX {name}",
                "constraint": f"ADD CONSTRAINT {name} ON {table}",
                "trigger": f"CREATE TRIGGER {name}",
            }[kind]
            subject = {
                "column": f"column {name!r} of {table}",
                "table": f"table {name!r}",
                "index": f"index {name!r} ON {table}",
                "constraint": f"constraint {name!r} ON {table}",
                "trigger": f"trigger {name!r} ON {table}",
            }[kind]
            # Fix example shown in the error message; plain text, never executed as SQL.
            stamp = f"INSERT INTO schema_migrations (name) VALUES ('{stem}')"  # noqa: S608
            errors.append(
                f"{stem}.sql: `{statement}` is not idempotent, but db/schema.sql "
                f"already contains the {subject} — a fresh DB replays unseeded "
                f"migrations and would fail here; stamp {stamp} in db/schema.sql, "
                f"or make the statement idempotent (IF NOT EXISTS / CREATE OR "
                f"REPLACE / a guarded DO block)"
            )
    return errors


def main() -> int:
    ups, downs, errors = _collect_migrations()
    errors.extend(_check_pairing(ups, downs))
    errors.extend(_check_schema_seed())
    errors.extend(_check_down_if_exists())
    errors.extend(_check_backfill_snapshot_drop_plans())
    errors.extend(_check_folded_strict_without_seed())

    if errors:
        print("migration lint failed:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1

    print(f"migration lint OK: {len(ups)} post-baseline migration(s), baseline seed aligned")
    return 0


if __name__ == "__main__":
    sys.exit(main())
