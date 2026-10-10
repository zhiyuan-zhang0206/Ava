"""Backup retention: `wal-g delete retain FULL 3`, only after the dry run proves it safe.

WAL-G decides what to delete (the oldest full backups beyond the retained count,
their increments, and the WAL older than the oldest retained backup). The deletion
is not reversible and the bucket has no versioning, so it is never run blind:

1. the same command runs first without `--confirm`; WAL-G lists every object it
   would delete (`Object marked for deletion: <key> storage=<name>` in its log);
2. the list is audited into the run's report, then three structural invariants must
   hold, each of which would be violated by a retention bug or a changed output
   format long before it deleted something that matters:
   - every key lies under `basebackups_005/` or `wal_005/` (nothing else in the
     bucket, including the other prefixes' data, can be touched);
   - no object belongs to the newest backup or to a backup it is an increment of;
   - at least one full backup survives;
3. only then the command runs again with `--confirm`, and the keys it deletes must
   be the keys that were audited.

Any violation aborts with nothing deleted. `wal-g delete garbage` is deliberately
not used: its behavior has not been exercised against this bucket layout.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from services.backup.walg.backups import Backup, BackupChainError, chain_of
from services.backup.walg.runner import WalgOutput, run_walg_logged

RETAIN_FULL_BACKUPS = 3
"""Full backups kept (design ruling: three weekly chains); their increments and the WAL
since the oldest of them go with them."""

DELETE_TIMEOUT_S = 3600
"""Bound for the dry run and the deletion; a bound, not an alert threshold."""

BACKUPS_PREFIX = "basebackups_005/"
WAL_PREFIX = "wal_005/"

_MARKED = re.compile(r"Object marked for deletion: (?P<key>\S+) storage=(?P<storage>\S+)")
_DRY_RUN_COUNT = re.compile(r"Dry run: objects would be deleted count=(?P<count>\d+)")
_DELETED_COUNT = re.compile(r"Objects deleted successfully: count=(?P<count>\d+)")
_SENTINEL_SUFFIX = "_backup_stop_sentinel.json"


class RetentionAbortedError(RuntimeError):
    """Retention stopped before deleting anything (or found the deletion differs from the audit)."""


@dataclass(frozen=True)
class RetentionOutcome:
    marked: int
    deleted: int


def parse_marked(output: WalgOutput, count_line: re.Pattern[str]) -> list[str]:
    """The object keys WAL-G listed as marked for deletion, from both of its streams.

    `count_line` matches WAL-G's own count of them: the dry run's "would be deleted"
    line or the confirmed run's "deleted successfully" line.

    Raises:
        RetentionAbortedError: the listing and WAL-G's own count of it disagree, or an
            object is on a storage other than the default one.
    """
    text = f"{output.stdout}\n{output.stderr}"
    keys: list[str] = []
    for match in _MARKED.finditer(text):
        if match["storage"] != "default":
            raise RetentionAbortedError(f"wal-g marked an object on storage {match['storage']!r}")
        keys.append(match["key"])
    counted = count_line.search(text)
    if keys and (counted is None or int(counted["count"]) != len(keys)):
        raise RetentionAbortedError(
            f"wal-g listed {len(keys)} objects but its own count differs or is missing"
        )
    return keys


def _belongs_to(key: str, backup_name: str) -> bool:
    return key == _sentinel(backup_name) or key.startswith(f"{BACKUPS_PREFIX}{backup_name}/")


def check_prefixes(keys: list[str]) -> None:
    """Invariant 1: every key lies under the backup or the WAL prefix."""
    outside = [key for key in keys if not key.startswith((BACKUPS_PREFIX, WAL_PREFIX))]
    if outside:
        raise RetentionAbortedError(
            f"retention would delete {len(outside)} objects outside {BACKUPS_PREFIX} "
            f"and {WAL_PREFIX}, e.g. {outside[0]}"
        )


def check_newest_chain_untouched(keys: list[str], backups: list[Backup]) -> None:
    """Invariant 2: no key belongs to the newest backup or to a backup it builds on."""
    if not backups:
        raise RetentionAbortedError(
            "there is no backup to keep, so retention has nothing to protect"
        )
    try:
        protected = chain_of(backups, backups[-1].name)
    except BackupChainError as exc:
        raise RetentionAbortedError(f"the newest backup's chain is not intact: {exc}") from None
    for backup in protected:
        touched = [key for key in keys if _belongs_to(key, backup.name)]
        if touched:
            raise RetentionAbortedError(
                f"retention would delete {backup.name}, which the newest backup "
                f"depends on, e.g. {touched[0]}"
            )


def check_a_full_backup_survives(keys: list[str], backups: list[Backup]) -> None:
    """Invariant 3: at least one full backup is left once the keys are gone."""
    gone = {backup.name for backup in backups if _sentinel(backup.name) in keys}
    if not [backup for backup in backups if backup.is_full and backup.name not in gone]:
        raise RetentionAbortedError("retention would delete every full backup")


def assert_invariants(keys: list[str], backups: list[Backup]) -> None:
    """Raise `RetentionAbortedError` unless deleting exactly `keys` is structurally safe.

    `backups` is the current list, newest last. The third invariant follows from the
    second for any real plan; it stays as the check that does not depend on the chain
    arithmetic being right.
    """
    check_prefixes(keys)
    check_newest_chain_untouched(keys, backups)
    check_a_full_backup_survives(keys, backups)


def _sentinel(backup_name: str) -> str:
    return f"{BACKUPS_PREFIX}{backup_name}{_SENTINEL_SUFFIX}"


def apply_retention(
    backups: list[Backup], report: Callable[[str], None], *, path_reader: Callable[[], Path | None]
) -> RetentionOutcome:
    """Delete what `retain FULL 3` expires, after auditing the dry run.

    `backups` is the current list, newest backup last. Nothing can expire until
    there are more full backups than are retained, so with fewer WAL-G is not asked.

    Raises:
        RetentionAbortedError: an invariant failed or the deletion differed from the audit.
        WalgCommandError: a wal-g call failed.
    """
    if sum(backup.is_full for backup in backups) <= RETAIN_FULL_BACKUPS:
        report(f"retention: {RETAIN_FULL_BACKUPS} or fewer full backups, nothing expires yet")
        return RetentionOutcome(marked=0, deleted=0)
    command = ["delete", "retain", "FULL", str(RETAIN_FULL_BACKUPS), "--use-sentinel-time"]
    planned = parse_marked(
        run_walg_logged(command, timeout_s=DELETE_TIMEOUT_S, path_reader=path_reader),
        _DRY_RUN_COUNT,
    )
    if not planned:
        report("retention: nothing to delete")
        return RetentionOutcome(marked=0, deleted=0)
    report(f"retention: dry run marks {len(planned)} objects for deletion:")
    for key in planned:
        report(f"  {key}")
    assert_invariants(planned, backups)
    deleted = parse_marked(
        run_walg_logged(
            [*command, "--confirm"], timeout_s=DELETE_TIMEOUT_S, path_reader=path_reader
        ),
        _DELETED_COUNT,
    )
    if sorted(deleted) != sorted(planned):
        raise RetentionAbortedError(
            f"the confirmed deletion ({len(deleted)} objects) is not the audited dry run "
            f"({len(planned)} objects)"
        )
    report(f"retention: deleted {len(deleted)} objects")
    return RetentionOutcome(marked=len(planned), deleted=len(deleted))
