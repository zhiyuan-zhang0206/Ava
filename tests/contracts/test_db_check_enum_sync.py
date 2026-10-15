from __future__ import annotations

import re
from pathlib import Path
from typing import get_args

import pytest

from base.agents.impersonation.status import ImpersonationStatus
from base.agents.messages.inbound import InboundKind
from base.agents.tasks.status import TaskStatus
from base.api_contracts.agent_contract import AgentStatus, LivenessState, TerminationSource
from base.cluster.machine import MachineRole
from base.packages.extensions.install_registry import TrustTier
from base.packages.extensions.registry import ExtensionKind

_SCHEMA = re.sub(
    r"--[^\n]*",
    "",
    (Path(__file__).resolve().parents[2] / "db" / "schema.sql").read_text(encoding="utf-8"),
)

_CASES: dict[tuple[str, str], set[str]] = {
    ("agents_meta", "status"): {s.value for s in AgentStatus},
    ("agents_meta", "liveness_state"): {s.value for s in LivenessState},
    ("agent_impersonations", "status"): {s.value for s in ImpersonationStatus},
    ("agents_meta", "termination_source"): {s.value for s in TerminationSource},
    ("inbound_messages", "kind"): {s.value for s in InboundKind},
    ("machines", "role"): set(get_args(MachineRole)),
    ("extensions", "kind"): set(get_args(ExtensionKind)),
    ("extensions", "trust"): set(get_args(TrustTier)),
    ("agent_tasks", "status"): {s.value for s in TaskStatus},
}


def _check_values(table: str, column: str) -> set[str]:
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
