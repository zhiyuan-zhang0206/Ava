"""Lock every Python enum / Literal that feeds a CHECK-constrained DB column to
its constraint's allowed-value set in `db/schema.sql`.

The bug class this guards: a Python value-set drifts from the DB `CHECK (col IN
(...))` it is written to, so a value valid in Python fails the constraint at
INSERT/UPDATE — a `CheckViolation` that only surfaces against a real database.
Unit tests that stub the write miss it entirely. Two incidents motivated this:

- `agents_meta.status`: code wrote `'starting'` but the docker CHECK was never
  ALTERed, so a startup UPDATE 500'd and the agent row petrified at 'allocated'.
- a cross-machine RPC kind drifting from its CHECK: a kind added to a Python
  Literal + the agent-runner dispatch but not the matching DB CHECK 500'd at the
  write in prod while every unit test stubbed the dispatch, so it shipped green.

Each pair below asserts the Python source-of-truth set EQUALS the schema.sql
CHECK set. A new value on either side without the other fails here. Adding a
value: (1) add it to the Python enum/Literal, (2) add it to `db/schema.sql`'s
CHECK, (3) ship a migration ALTERing the live constraint. This test verifies (1)
and (2) are in sync; the migration + `scripts/lint_migrations.py` cover (3).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import get_args

import pytest

from shared.agents import AgentStatus, TerminationSource
from shared.inbound import InboundKind
from shared.machine import MachineRole

# Strip `-- ...` line comments first: the CHECK value lists carry inline comments
# whose prose can contain `)` (e.g. "ava.compact(summary)"), which would otherwise
# truncate the IN-list capture below.
_SCHEMA = re.sub(
    r"--[^\n]*",
    "",
    (Path(__file__).resolve().parents[1] / "db" / "schema.sql").read_text(encoding="utf-8"),
)

# (table, column) -> the Python value-set that is the source of truth for it.
_CASES: dict[tuple[str, str], set[str]] = {
    ("agents_meta", "status"): {s.value for s in AgentStatus},
    # The stamped-by-every-terminated-write source. A value in the enum but not the
    # CHECK is a CheckViolation at the write site; a value in the CHECK but not the
    # enum is one scripts/lint_termination_source.py would reject as unknown.
    ("agents_meta", "termination_source"): {s.value for s in TerminationSource},
    ("inbound_messages", "kind"): {s.value for s in InboundKind},
    ("machines", "role"): set(get_args(MachineRole)),
}


def _check_values(table: str, column: str) -> set[str]:
    """The allowed-value set of `column`'s CHECK clause, scoped to `table`'s
    `CREATE TABLE` block in db/schema.sql. Handles both a scalar `CHECK (column IN
    (...))` and a set-valued `CHECK (column <@ ARRAY[...])` (TEXT[] columns)."""
    block = re.search(
        rf"CREATE TABLE (?:IF NOT EXISTS )?{re.escape(table)}\s*\((.*?)\n\);",
        _SCHEMA,
        re.DOTALL,
    )
    assert block is not None, (
        f"no CREATE TABLE block for {table!r} in db/schema.sql — the schema format "
        f"this test assumes changed; update the regex."
    )
    clause = re.search(
        rf"CHECK\s*\(\s*{re.escape(column)}\s+IN\s*\(([^)]+)\)",
        block.group(1),
        re.DOTALL,
    ) or re.search(
        rf"CHECK\s*\(\s*{re.escape(column)}\s+<@\s+ARRAY\s*\[([^\]]+)\]",
        block.group(1),
        re.DOTALL,
    )
    assert clause is not None, (
        f"no `CHECK ({column} IN (...))` or `CHECK ({column} <@ ARRAY[...])` in "
        f"{table!r}'s DDL — schema format changed."
    )
    return {v.strip().strip("'") for v in clause.group(1).split(",") if v.strip()}


@pytest.mark.parametrize(("table", "column"), list(_CASES))
def test_python_value_set_matches_db_check(table: str, column: str) -> None:
    python_set = _CASES[(table, column)]
    db_set = _check_values(table, column)
    assert python_set == db_set, (
        f"{table}.{column}: the Python source-of-truth {sorted(python_set)} != the "
        f"db/schema.sql CHECK {sorted(db_set)}. A value in one but not the other "
        f"means an INSERT/UPDATE of it fails the constraint (or a dead CHECK entry). "
        f"Sync both sides + ship a migration ALTERing the live constraint."
    )
