---
type: doc
title: "Schema drift audit"
description: "scripts/audit/schema_drift.py compares the live database's public columns with the db/schema.sql baseline, classifies every difference and exits nonzero while any remains."
tags: [scripts, audit, database]
---

# Schema drift audit

`scripts/audit/schema_drift.py` answers one question: does the live database
still match the baseline `db/schema.sql`? It parses the baseline without a
database, reads `information_schema.columns` from the target, and reports every
difference in both directions. Run it from the repository root:

```
.venv/bin/python -m scripts.audit.schema_drift [--schema FILE] [--dsn URL] [--json OUT] [--allow FILE]
```

Without `--dsn` it takes the deployment settings' data-plane URL. The connection
is opened read-only: the tool writes neither the database nor `db/schema.sql`.

The residue it exists for: production carried `agents_meta.watchers` — `jsonb
NOT NULL DEFAULT '[]'`, thousands of rows, no reader, no writer, no migration,
absent from the baseline. A design discussion about that column started from a
false premise until someone diffed the two schemas by hand; this script repeats
that diff on demand. 2026-10-17 against the production database it reported one
extra column (`agents_meta.watchers`), ten extra tables — the four checkpointer
tables and six `telemetry_events` partitions — and nothing missing.

## schema-drift

The report is one line per difference: extra table, extra column, missing table,
missing column, or `clean`. Table names compare case-insensitively; a column is
extra when the database has it and the baseline does not, and missing in the
reverse direction, so both a declared-only and a live-only object surface.

Exit code `1` means drift remains, `0` means the two sides agree. The code is
the whole contract for a scheduled or manual check: no drift, no noise.

`--json OUT` writes the same findings as objects carrying their class, for a
report or a diff between two runs. `--allow FILE` names entries to ignore, one
`table` or `table.column` per line; it subtracts from the extra tables and
columns before the report and before the exit code, so a known and accepted residue does not
keep the check red. The allowlist is per invocation, never a committed
default, and it cannot silence a missing object — a baseline entry that the
database lacks stays a failure.

```meiva-contract
id = "schema-drift"
kind = "invariant"
statement = "The audit reports extra and missing tables and columns in both directions, writes the same findings with their classes as JSON on request, subtracts an explicit per-run allowlist of table and table.column entries from the extra objects only, and exits 1 while any difference remains and 0 when the two sides agree."
code = ["scripts/audit/schema_drift.py:<module>", "scripts/audit/schema_drift.py:compare", "scripts/audit/schema_drift.py:main"]
tests = ["scripts/audit/tests/test_schema_drift.py::test_compare_reports_extra_and_missing_tables", "scripts/audit/tests/test_schema_drift.py::test_compare_reports_a_declared_column_absent_from_the_database", "scripts/audit/tests/test_schema_drift.py::test_render_prints_clean_without_drift"]
```

## Baseline parsing

The baseline is the squashed schema plus later `ALTER TABLE ... ADD COLUMN`
deltas, and it carries line and block comments and table-level constraints. The
parser strips comments outside string literals, reads each `CREATE TABLE` body
to its matching closing parenthesis, and skips a part whose first token is a
constraint keyword (`UNIQUE (...)`, `CONSTRAINT ... PRIMARY KEY (...)`,
`CHECK (...)`, `LIKE ...`). A part whose first token is an identifier is a
column even when the name is `key`, `primary` or `check` in another spelling —
that case is the trap: treating a keyword-looking column as a constraint
silently loses a declaration and turns a clean database into "extra column".
`ALTER TABLE ... ADD COLUMN [IF NOT EXISTS]` adds to the table it names.

```meiva-contract
id = "baseline-parsing"
kind = "invariant"
statement = "Baseline parsing strips comments outside string literals, reads a CREATE TABLE body to its matching closing parenthesis, skips only the parts whose first token is a constraint keyword so a column named key stays a column, and merges every ALTER TABLE ADD COLUMN delta into the table it names."
code = ["scripts/audit/schema_drift.py:strip_comments", "scripts/audit/schema_drift.py:schema_columns"]
tests = ["scripts/audit/tests/test_schema_drift.py::test_strip_comments_keeps_dashes_inside_a_string_literal", "scripts/audit/tests/test_schema_drift.py::test_schema_columns_skips_table_level_constraints", "scripts/audit/tests/test_schema_drift.py::test_schema_columns_keeps_a_column_named_key", "scripts/audit/tests/test_schema_drift.py::test_schema_columns_applies_alter_table_add_column"]
```

## Drift classes

Every finding carries a class so a reader can separate designed runtime objects
from residue:

- `design/runtime-partition` — an extra table named `<declared table>_<six
  digits>` or `<declared table>_default`, a partition created at runtime by
  `base/telemetry/event_store.py::ensure_partitions` under a table the baseline
  does declare.
- `design/runtime-ddl` — an extra table whose name starts with a registered
  runtime DDL prefix (`checkpoint` today: the four checkpointer tables the
  checkpointer creates itself).
- `dangerous` — an extra column that is `NOT NULL` with no default: an existing
  row or an older writer fails once it is written.
- `historical` — everything else, including a defaulted `NOT NULL` column such
  as `agents_meta.watchers`: residue worth a decision, not an immediate outage.

A runtime-owned table is invisible to this classification until its prefix joins
the registered prefixes in `schema_drift.py`; that constant is the explicit
registration, and an unregistered table reports as `historical`, which is the
truthful answer — drift nobody has explained yet.

```meiva-contract
id = "drift-classes"
kind = "invariant"
statement = "An extra table is design/runtime-partition only when its parent is a declared table and its suffix is six digits or default, design/runtime-ddl only for a registered runtime DDL prefix, and historical otherwise; an extra column is dangerous only when it is NOT NULL without a default."
code = ["scripts/audit/schema_drift.py:classify_table", "scripts/audit/schema_drift.py:classify_column"]
tests = ["scripts/audit/tests/test_schema_drift.py::test_classify_table_recognises_a_partition_of_a_declared_parent", "scripts/audit/tests/test_schema_drift.py::test_classify_table_does_not_invent_a_class_for_an_undeclared_parent", "scripts/audit/tests/test_schema_drift.py::test_classify_table_recognises_an_undeclared_runtime_ddl_table", "scripts/audit/tests/test_schema_drift.py::test_classify_column_flags_a_required_column_without_a_default", "scripts/audit/tests/test_schema_drift.py::test_classify_column_treats_a_defaulted_column_as_historical"]
```

Parent: [[scripts/audit/docs/audit.meiva.okf.md]].
