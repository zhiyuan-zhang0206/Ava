"""No production code writes `agents_meta.status = 'restarting'`.

The update straggler reap was the last writer of the value
(`decisions/2026-09-30-remove-straggler-reap.md`). It stays in the
`AgentStatus` enum and the column's CHECK until the schema is cleaned up, so
the readers that classify an old row keep working; this guard keeps a new
writer from reviving the state unnoticed.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]

# Production trees only, mirroring the crash-row writer guard's scope.
_SCAN_DIRS = ("agent", "ava", "ava_builtins", "cli", "gateway", "ops", "services", "base")

_SET_RESTARTING = re.compile(r"SET\s+status\s*=\s*['\"]restarting['\"]", re.IGNORECASE)
_ENUM_MEMBER = re.compile(r"\bRESTARTING\b")

# The enum member's definition and its one reader (the heartbeat projection of
# idle-family statuses).
_ENUM_MEMBER_FILES = {"base/agents/contract.py", "gateway/inspect/_live.py"}


def _sources() -> dict[str, str]:
    return {
        path.relative_to(_REPO).as_posix(): path.read_text(encoding="utf-8")
        for directory in _SCAN_DIRS
        for path in sorted((_REPO / directory).rglob("*.py"))
        if "tests" not in path.relative_to(_REPO).parts  # a package's own tests/ is not production
    }


def test_no_statement_sets_the_restarting_status() -> None:
    writers = sorted(rel for rel, text in _sources().items() if _SET_RESTARTING.search(text))
    assert writers == []


def test_the_restarting_enum_member_is_defined_and_read_but_never_written() -> None:
    users = {rel for rel, text in _sources().items() if _ENUM_MEMBER.search(text)}
    assert users == _ENUM_MEMBER_FILES
