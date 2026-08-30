"""Load schema-v1 eval cases from JSONL evalset files.

The shared contract is documented in `evals/SCHEMA-v1.md`. This module is the
reference loader: `load_evalset` parses a file into :class:`EvalCaseV1`
records and validates every row loudly, so a malformed evalset fails at load
time instead of halfway through a run.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, cast

REQUIRED_FIELDS = ("id", "input", "expected", "grader", "meta")
GRADER_TYPES = frozenset({"artifact-audit", "llm-judge", "exact", "custom"})
LINE_VALUES = frozenset({"ava", "monsora", "speechful", "research"})
SCHEMA_VERSION = "1"
_SEGMENT = re.compile(r"^[a-z0-9]+$")
_SEQ = re.compile(r"^[a-z]*\d{3}$")


@dataclass(frozen=True)
class EvalCaseV1:
    """One schema-v1 eval case row."""

    id: str
    input: dict[str, object]
    expected: dict[str, object]
    grader: dict[str, object]
    meta: dict[str, object]


def load_evalset(path: Path) -> list[EvalCaseV1]:
    """Parse and validate a schema-v1 JSONL evalset file.

    Blank lines and ``#``-prefixed comment lines are skipped. Raises
    :class:`ValueError` with a ``file:line`` prefix on the first malformed
    row.
    """
    cases: list[EvalCaseV1] = []
    seen_ids: set[str] = set()
    with path.open(encoding="utf-8") as handle:
        for lineno, raw in enumerate(handle, start=1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            row = _parse_row(line, path, lineno)
            _validate_row(row, path, lineno, seen_ids)
            cases.append(_from_row(row))
    return cases


def _parse_row(line: str, path: Path, lineno: int) -> Any:
    try:
        row = json.loads(line)
    except json.JSONDecodeError as error:
        raise ValueError(f"{path}:{lineno}: invalid JSON: {error}") from error
    if not isinstance(row, dict):
        raise ValueError(f"{path}:{lineno}: row must be a JSON object")  # noqa: TRY004 — one error type for malformed data files
    return cast(dict[str, Any], row)


def _validate_row(row: Any, path: Path, lineno: int, seen_ids: set[str]) -> None:
    for field in REQUIRED_FIELDS:
        if field not in row:
            raise ValueError(f"{path}:{lineno}: missing required field {field!r}")
    case_id = row["id"]
    if not isinstance(case_id, str):
        raise ValueError(f"{path}:{lineno}: id must be a string, got {case_id!r}")  # noqa: TRY004 — one error type for malformed data files
    _validate_id(case_id, path, lineno)
    if case_id in seen_ids:
        raise ValueError(f"{path}:{lineno}: duplicate id {case_id!r}")
    seen_ids.add(case_id)
    for field in ("input", "expected", "grader", "meta"):
        if not isinstance(row[field], dict):
            raise ValueError(f"{path}:{lineno}: {field} must be an object")  # noqa: TRY004 — one error type for malformed data files
    grader = row["grader"]
    if grader.get("type") not in GRADER_TYPES:
        raise ValueError(f"{path}:{lineno}: unknown grader type {grader.get('type')!r}")
    if not isinstance(grader.get("impl"), str) or not grader["impl"]:
        raise ValueError(f"{path}:{lineno}: grader.impl must be a non-empty string")
    if not isinstance(grader.get("grader_version"), str) or not grader["grader_version"]:
        raise ValueError(f"{path}:{lineno}: grader.grader_version must be a non-empty string")
    if not isinstance(row["expected"].get("facts"), dict):
        raise ValueError(f"{path}:{lineno}: expected.facts must be an object (structured facts)")  # noqa: TRY004 — one error type for malformed data files
    meta = row["meta"]
    if meta.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(
            f"{path}:{lineno}: meta.schema_version must be {SCHEMA_VERSION!r}, "
            f"got {meta.get('schema_version')!r}"
        )
    line = meta.get("line")
    if line not in LINE_VALUES:
        raise ValueError(
            f"{path}:{lineno}: meta.line must be one of {sorted(LINE_VALUES)}, got {line!r}"
        )
    if line != case_id.split("-", 1)[0]:
        raise ValueError(
            f"{path}:{lineno}: meta.line must equal the id's first segment, "
            f"got {line!r} for id {case_id!r}"
        )
    if not isinstance(meta.get("family"), str) or not meta["family"]:
        raise ValueError(f"{path}:{lineno}: meta.family must be a non-empty string")
    created_at = meta.get("created_at")
    if not isinstance(created_at, str) or not created_at:
        raise ValueError(f"{path}:{lineno}: meta.created_at must be a non-empty string")
    try:
        parsed = datetime.fromisoformat(created_at)
    except ValueError as error:
        raise ValueError(
            f"{path}:{lineno}: meta.created_at must be ISO-8601 with an explicit offset, "
            f"got {created_at!r}"
        ) from error
    if parsed.utcoffset() is None:
        raise ValueError(
            f"{path}:{lineno}: meta.created_at must carry an explicit UTC offset, "
            f"got {created_at!r}"
        )


def _validate_id(case_id: str, path: Path, lineno: int) -> None:
    parts = case_id.split("-")
    # <line>-<domain>[-<more domains>]-<seq>: seq carries a fixed-width
    # three-digit number (c001, never c1/c01); earlier segments are
    # lowercase alphanumeric (the line and domain vocabulary).
    if len(parts) < 3:
        raise ValueError(
            f"{path}:{lineno}: id must have <line>-<domain>-<seq> segments (at least 3), got {case_id!r}"
        )
    if not _SEQ.match(parts[-1]):
        raise ValueError(
            f"{path}:{lineno}: id seq segment must end in a fixed-width "
            f"3-digit number, got {case_id!r}"
        )
    for segment in parts[:-1]:
        if not _SEGMENT.match(segment):
            raise ValueError(
                f"{path}:{lineno}: id segment {segment!r} must be lowercase alphanumeric"
            )


def _from_row(row: Any) -> EvalCaseV1:
    return EvalCaseV1(
        id=row["id"],
        input=row["input"],
        expected=row["expected"],
        grader=row["grader"],
        meta=row["meta"],
    )
