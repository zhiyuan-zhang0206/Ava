#!/usr/bin/env python3
# meiva:link scripts/audit/docs/schema-drift.meiva.okf.md

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psycopg

_CONSTRAINT = frozenset({"CONSTRAINT", "PRIMARY", "UNIQUE", "CHECK", "FOREIGN", "EXCLUDE", "LIKE"})
_PARTITION = re.compile(r"^(?P<parent>.+)_(?P<suffix>\d{6}|default)$")
_RUNTIME_DDL = ("checkpoint",)
_ADD_COLUMN = re.compile(
    r"ALTER\s+TABLE\s+(?:ONLY\s+)?(?P<table>[\w.\"]+)\s+ADD\s+COLUMN\s+"
    r"(?:IF\s+NOT\s+EXISTS\s+)?(?P<column>[\w\"]+)",
    re.IGNORECASE,
)


@dataclass
class Drift:
    extra_tables: list[str] = field(default_factory=list[str])
    missing_tables: list[str] = field(default_factory=list[str])
    extra_columns: list[tuple[str, str, str]] = field(default_factory=list[tuple[str, str, str]])
    missing_columns: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])


def strip_comments(sql: str) -> str:
    out: list[str] = []
    i = 0
    in_str = False
    while i < len(sql):
        if not in_str and sql.startswith("--", i):
            end = sql.find("\n", i)
            i = len(sql) if end < 0 else end
            continue
        if not in_str and sql.startswith("/*", i):
            end = sql.find("*/", i + 2)
            i = len(sql) if end < 0 else end + 2
            continue
        if sql[i] == "'":
            in_str = not in_str
        out.append(sql[i])
        i += 1
    return "".join(out)


def _split_top_level(body: str) -> list[str]:
    parts: list[str] = []
    depth = 0
    in_str = False
    current: list[str] = []
    for char in body:
        if char == "'":
            in_str = not in_str
        if not in_str:
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
            elif char == "," and depth == 0:
                parts.append("".join(current))
                current = []
                continue
        current.append(char)
    parts.append("".join(current))
    return parts


# meiva:link scripts/audit/docs/schema-drift.meiva.okf.md
def schema_columns(sql: str) -> dict[str, list[str]]:
    text = strip_comments(sql)
    tables: dict[str, list[str]] = {}
    for match in re.finditer(
        r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(?P<name>[\w\"]+)\s*\(",
        text,
        re.IGNORECASE,
    ):
        name = match.group("name").strip('"')
        start = match.end()
        depth = 1
        in_str = False
        index = start
        while index < len(text) and depth > 0:
            char = text[index]
            if char == "'":
                in_str = not in_str
            elif not in_str:
                if char == "(":
                    depth += 1
                elif char == ")":
                    depth -= 1
            index += 1
        body = text[start : index - 1]
        columns: list[str] = []
        for raw in _split_top_level(body):
            part = raw.strip()
            if not part:
                continue
            token = part.split()[0].strip('"')
            if token.upper() in _CONSTRAINT or "(" in token:
                continue
            columns.append(token)
        tables[name] = columns
    for match in _ADD_COLUMN.finditer(text):
        table = match.group("table").strip('"')
        column = match.group("column").strip('"')
        tables.setdefault(table, [])
        if column not in tables[table]:
            tables[table].append(column)
    return tables


def live_columns(conn: psycopg.Connection[Any]) -> dict[str, dict[str, str]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT table_name, column_name, is_nullable, column_default "
            "FROM information_schema.columns WHERE table_schema = 'public'"
        )
        out: dict[str, dict[str, str]] = {}
        for table, column, nullable, default in cur.fetchall():
            out.setdefault(table, {})[column] = f"{nullable}|{default or ''}"
        return out


def compare(schema: dict[str, list[str]], live: dict[str, dict[str, str]]) -> Drift:
    schema_lower = {name.lower(): cols for name, cols in schema.items()}
    live_lower = {name.lower(): cols for name, cols in live.items()}
    drift = Drift()
    drift.extra_tables = sorted(set(live_lower) - set(schema_lower))
    drift.missing_tables = sorted(set(schema_lower) - set(live_lower))
    for table in sorted(set(schema_lower) & set(live_lower)):
        declared = {column.lower() for column in schema_lower[table]}
        present = {column.lower() for column in live_lower[table]}
        for column in sorted(present - declared):
            drift.extra_columns.append((table, column, live_lower[table][column]))
        for column in sorted(declared - present):
            drift.missing_columns.append((table, column))
    return drift


# meiva:link scripts/audit/docs/schema-drift.meiva.okf.md
def classify_table(name: str, declared: set[str]) -> str:
    match = _PARTITION.match(name)
    if match and match.group("parent") in declared:
        return "design/runtime-partition"
    if name.startswith(_RUNTIME_DDL):
        return "design/runtime-ddl"
    return "historical"


def classify_column(meta: str) -> str:
    nullable, _, default = meta.partition("|")
    if nullable == "NO" and not default:
        return "dangerous"
    return "historical"


def render(drift: Drift, declared: set[str]) -> str:
    lines = ["# schema drift", ""]
    for name in drift.extra_tables:
        lines.append(f"extra table   {name}  [{classify_table(name, declared)}]")
    for table, column, meta in drift.extra_columns:
        lines.append(f"extra column  {table}.{column}  [{classify_column(meta)}]")
    for name in drift.missing_tables:
        lines.append(f"missing table {name}")
    for table, column in drift.missing_columns:
        lines.append(f"missing column {table}.{column}")
    if len(lines) == 2:
        lines.append("clean")
    return "\n".join(lines)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compare live columns against db/schema.sql.")
    parser.add_argument("--schema", type=Path, default=Path("db/schema.sql"))
    parser.add_argument(
        "--dsn", default=None, help="Postgres URL; defaults to the deployment settings"
    )
    parser.add_argument("--json", type=Path, default=None)
    parser.add_argument(
        "--allow",
        type=Path,
        default=None,
        help="file listing table names and table.column entries to ignore",
    )
    return parser.parse_args(argv)


def _dsn(args: argparse.Namespace) -> str:
    if args.dsn is not None:
        return str(args.dsn)
    from base.config import settings

    return settings.data_plane.db_url


def _allowlisted(drift: Drift, allow: Path | None) -> Drift:
    if allow is None or not allow.exists():
        return drift
    allowed = {
        line.strip() for line in allow.read_text(encoding="utf-8").splitlines() if line.strip()
    }
    drift.extra_tables = [name for name in drift.extra_tables if name not in allowed]
    drift.extra_columns = [
        column for column in drift.extra_columns if f"{column[0]}.{column[1]}" not in allowed
    ]
    return drift


def _as_json(drift: Drift, declared: set[str]) -> dict[str, Any]:
    return {
        "extra_tables": [
            {"name": name, "class": classify_table(name, declared)} for name in drift.extra_tables
        ],
        "extra_columns": [
            {"table": table, "column": column, "class": classify_column(meta)}
            for table, column, meta in drift.extra_columns
        ],
        "missing_tables": drift.missing_tables,
        "missing_columns": drift.missing_columns,
    }


def _is_dirty(drift: Drift) -> bool:
    return bool(
        drift.extra_tables or drift.extra_columns or drift.missing_tables or drift.missing_columns
    )


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    schema = schema_columns(args.schema.read_text(encoding="utf-8"))
    with psycopg.connect(_dsn(args)) as conn:
        conn.read_only = True
        live = live_columns(conn)
    declared = {name.lower() for name in schema}
    drift = _allowlisted(compare(schema, live), args.allow)
    print(render(drift, declared))
    if args.json is not None:
        args.json.write_text(json.dumps(_as_json(drift, declared), indent=2), encoding="utf-8")
    return int(_is_dirty(drift))


if __name__ == "__main__":
    sys.exit(main())
