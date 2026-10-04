"""The backups in the bucket, as `wal-g backup-list --detail --json` reports them.

A WAL-G backup name is `base_<start segment>` for a full backup and
`base_<start segment>_D_<parent's start segment>` for an increment of the backup
that started at that segment. The chain behind an increment is therefore readable
from the names alone, which is what retention needs to prove it never strands the
newest backup.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

from services.backup.walg.runner import run_walg

QUERY_TIMEOUT_S = 1800
"""Bound for listing and verifying calls: a hung call must not hold the tick's lock.
A bound, not an alert threshold."""

_DELTA_MARK = "_D_"


class BackupChainError(RuntimeError):
    """The backup list does not describe a restorable chain."""


@dataclass(frozen=True)
class Backup:
    name: str
    start_time: datetime
    uncompressed_bytes: int
    compressed_bytes: int
    start_lsn: int  # where the backup's WAL begins (an integer position, `0/2000028` is 33554472)
    finish_lsn: int  # the position `pg_backup_stop` returned: replay must reach it

    @property
    def is_full(self) -> bool:
        return _DELTA_MARK not in self.name

    @property
    def parent_segment(self) -> str | None:
        """Start segment of the backup this increment builds on; None for a full backup."""
        return None if self.is_full else self.name.split(_DELTA_MARK, 1)[1]


def parse_backups(text: str) -> list[Backup]:
    """Backups oldest first by start time. Empty output and JSON `null` mean none.

    Raises:
        BackupChainError: the output is not the expected JSON list of backups.
    """
    if not text.strip():
        return []
    try:
        payload: Any = json.loads(text)
    except ValueError:
        raise BackupChainError("backup-list printed output that is not JSON") from None
    if payload is None:
        return []
    if not isinstance(payload, list):
        raise BackupChainError("backup-list printed JSON that is not a list")
    backups: list[Backup] = []
    for raw in cast(list[Any], payload):
        try:
            entry = cast(dict[str, Any], raw)
            backups.append(
                Backup(
                    name=str(entry["backup_name"]),
                    start_time=datetime.fromisoformat(str(entry["start_time"])),
                    uncompressed_bytes=int(entry["uncompressed_size"]),
                    compressed_bytes=int(entry["compressed_size"]),
                    start_lsn=int(entry["start_lsn"]),
                    finish_lsn=int(entry["finish_lsn"]),
                )
            )
        except (KeyError, TypeError, ValueError):
            raise BackupChainError("backup-list printed an entry of an unexpected shape") from None
    return sorted(backups, key=lambda backup: backup.start_time)


def list_backups() -> list[Backup]:
    """Every backup under the configured prefix, oldest first."""
    text = run_walg(["backup-list", "--detail", "--json"], timeout_s=QUERY_TIMEOUT_S)
    return parse_backups(text)


def chain_of(backups: list[Backup], name: str) -> list[Backup]:
    """`name` and every backup it depends on, newest first, ending at a full backup.

    Raises:
        BackupChainError: `name` or one of its parents is not in `backups`.
    """
    by_name = {backup.name: backup for backup in backups}
    if name not in by_name:
        raise BackupChainError(f"backup {name} is not in the backup list")
    chain = [by_name[name]]
    while (segment := chain[-1].parent_segment) is not None:
        parents = [
            backup
            for backup in backups
            if backup.name == f"base_{segment}" or backup.name.startswith(f"base_{segment}_D_")
        ]
        if len(parents) != 1:
            raise BackupChainError(
                f"the parent of {chain[-1].name} (segment {segment}) "
                f"matches {len(parents)} backups, not one"
            )
        chain.append(parents[0])
    return chain
