"""Append-only audit journal for policy-owned retention actions.

Every retention action is recorded before its effect and after its result:
gate transitions (the explicit arm/disable commands) and per-object
intent -> result pairs of one execution tick. The journal is the audit
trail the safety design leans on (task #2150, design section 4): JSONL,
one record per line, under ``$AVA_HOME/physical-backup/retention-journal/``,
opened append-only with each record fsynced before the next action.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

_JOURNAL_NAME = "journal.jsonl"


class RetentionJournal:
    """One append-only JSONL journal file under its dedicated directory."""

    def __init__(self, directory: Path) -> None:
        self._directory = directory

    def append(self, kind: str, fields: Mapping[str, object]) -> None:
        """Append one timestamped record; the record is fsynced before return."""

        record: dict[str, object] = {
            "at": datetime.now(UTC).isoformat(),
            "kind": kind,
            **fields,
        }
        line = json.dumps(record, sort_keys=True, separators=(",", ":")).encode() + b"\n"
        self._directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self._directory / _JOURNAL_NAME
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, line)
            os.fsync(fd)
        finally:
            os.close(fd)
