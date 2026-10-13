"""Recorded locality debt: the baseline format, its keys and the `[debt]` text of a manifest.

A baseline lives in the `meizel.toml` of the directory that judges the violation, so debt is as
local as the rule: an edge violation sits in the lowest common directory of the edge, a
restricted form in the directory of its file. Entries carry no line numbers, so code that moves
inside a file leaves the baseline alone.

```toml
[debt.private-name]
"test_a.py -> a.py#_helper" = 3

[debt.manifest-cycle]
"b.py -> a.py" = 1
```
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from typing import cast

from meizel.graph.model import Code

__all__ = [
    "CYCLE",
    "DEBTABLE",
    "TABLE_KEY",
    "DebtTable",
    "back_edge",
    "edge_key",
    "file_key",
    "listing",
    "owner_hint",
    "parse_debt",
    "render_debt",
    "set_debt",
    "source_of",
    "total",
]

CYCLE = Code.MANIFEST_CYCLE
_EDGE_CODES = frozenset(
    {Code.UNDECLARED_EDGE, Code.NOT_EXPORTED, Code.PRIVATE_MODULE, Code.PRIVATE_NAME}
)
_FILE_CODES = frozenset({Code.RESTRICTED, Code.FIRST_PARTY_PATCH})
DEBTABLE = _EDGE_CODES | _FILE_CODES | {CYCLE}
TABLE_KEY = "debt"

type DebtTable = dict[Code, dict[str, int]]

_NAMED = frozenset({Code.PRIVATE_NAME})
_SEPARATOR = " -> "
_DEBT_HEADER = re.compile(r"^\s*\[\s*debt\s*(?:\.[^\]]*)?\]\s*(?:#.*)?$")
_ANY_HEADER = re.compile(r"^\s*\[[^\]]*\]\s*(?:#.*)?$")


def _within(owner: str, path: str) -> str:
    return path[len(owner) + 1 :] if owner else path


def edge_key(owner: str, src: str, dst: str, name: str | None = None) -> str:
    """`src -> dst`, or `src -> dst#name` for a private name, relative to the owning directory."""
    key = f"{_within(owner, src)}{_SEPARATOR}{_within(owner, dst)}"
    return f"{key}#{name}" if name else key


def file_key(owner: str, path: str, what: str) -> str:
    """`file#what`, relative to the owning directory."""
    return f"{_within(owner, path)}#{what}"


def source_of(owner: str, code: Code, key: str) -> str | None:
    """The repository path of the file a debt is about; None for a cycle back-edge."""
    if code is CYCLE:
        return None
    head = key.split(_SEPARATOR, 1)[0] if code in _EDGE_CODES else key.split("#", 1)[0]
    return f"{owner}/{head}" if owner else head


def back_edge(key: str) -> tuple[str, str]:
    """The `(from, to)` children of a `manifest-cycle` key."""
    src, _, dst = key.partition(_SEPARATOR)
    return src, dst


def _edge_problem(code: Code, key: str) -> str | None:
    head, _, name = key.partition("#")
    src, _, dst = head.partition(_SEPARATOR)
    if not src or not dst:
        return "an edge debt is written `source -> target`"
    if code in _NAMED and not name:
        return "a private-name debt is written `source -> target#name`"
    if code not in _NAMED and name:
        return f"a {code} debt is written `source -> target` with no name"
    return None


def _key_problem(code: Code, key: str) -> str | None:
    if code is CYCLE:
        src, dst = back_edge(key)
        ok = bool(src) and bool(dst) and "/" not in key and src != dst
        return None if ok else "a cycle back-edge is written `child -> child`"
    if code in _EDGE_CODES:
        return _edge_problem(code, key)
    file, _, what = key.partition("#")
    return None if file and what else f"a {code} debt is written `file#form`"


def parse_debt(table: object, report: Callable[[str], None]) -> DebtTable:
    """Validate a parsed `[debt]` table; every defect goes to `report`, valid entries are kept."""
    if not isinstance(table, dict):
        report("[debt] must be a table")
        return {}
    result: DebtTable = {}
    for name, rows in cast("dict[str, object]", table).items():
        code = next((c for c in DEBTABLE if c.value == name), None)
        if code is None:
            known = ", ".join(sorted(c.value for c in DEBTABLE))
            report(f"[debt.{name}] is not a code that can be recorded; the codes are {known}")
        elif not isinstance(rows, dict):
            report(f"[debt.{name}] must be a table of `key = count`")
        else:
            kept = _valid_entries(code, cast("dict[str, object]", rows), report)
            if kept:
                result[code] = kept
    return result


def _valid_entries(
    code: Code, rows: Mapping[str, object], report: Callable[[str], None]
) -> dict[str, int]:
    kept: dict[str, int] = {}
    for key, count in rows.items():
        problem = _key_problem(code, key)
        if problem is not None:
            report(f"[debt.{code}] {key!r}: {problem}")
        elif not isinstance(count, int) or isinstance(count, bool) or count < 1:
            report(f"[debt.{code}] {key!r}: the count must be a positive integer")
        elif code is CYCLE and count != 1:
            report(f"[debt.{code}] {key!r}: a back-edge is recorded once; the count must be 1")
        else:
            kept[key] = count
    return kept


def listing(table: DebtTable) -> list[tuple[Code, str, int]]:
    """Every `(code, key, count)` in baseline order: by code, then by key."""
    return [
        (code, key, n) for code in sorted(table, key=str) for key, n in sorted(table[code].items())
    ]


def total(table: DebtTable) -> int:
    return sum(count for rows in table.values() for count in rows.values())


def owner_hint(owner: str, table: DebtTable) -> str:
    """Why a violation in a directory that carries debt fails: it is strict-to-be."""
    where = owner or "."
    return (
        f"{where} is strict-to-be: it carries {total(table)} recorded locality debts that must only "
        "shrink. Fix this violation rather than recording it; `python -m meizel debt record` "
        "exists for deliberate exceptions."
    )


def render_debt(table: DebtTable) -> str:
    """The `[debt.<code>]` tables for `table`, sorted, one debt per line; empty when none."""
    blocks = [
        f"[debt.{code}]\n"
        + "\n".join(
            f"{json.dumps(key, ensure_ascii=False)} = {count}"
            for key, count in sorted(table[code].items())
        )
        for code in sorted(table, key=str)
        if table[code]
    ]
    return "\n\n".join(blocks) + "\n" if blocks else ""


def set_debt(text: str, table: DebtTable) -> str:
    """`text` with its `[debt.*]` tables replaced by `table`; everything else is kept as written."""
    kept: list[str] = []
    inside = False
    for line in text.splitlines():
        if _DEBT_HEADER.match(line):
            inside = True
        elif _ANY_HEADER.match(line):
            inside = False
        if not inside:
            kept.append(line)
    while kept and not kept[-1].strip():
        kept.pop()
    base = "\n".join(kept)
    debt = render_debt(table)
    if not debt:
        return base + "\n" if base else ""
    return f"{base}\n\n{debt}" if base else debt
