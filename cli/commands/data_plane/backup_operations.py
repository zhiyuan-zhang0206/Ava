"""`ava backup operations`: custody status and retirement of the scheduled backup kinds."""

from __future__ import annotations

import sys

from services.backup.scheduler.operation.custody import (
    OperationKind,
    blocked_operations,
    quarantine_entries,
    retire_blocked,
)
from services.backup.scheduler.worker import dump_kind, restore_drill_kind


def _operation_kinds() -> list[OperationKind]:
    return [dump_kind(), restore_drill_kind()]


def cmd_backup_operations_status() -> int:
    """Show which operation kinds are blocked and what quarantine holds."""
    kinds = _operation_kinds()
    blocked_any = False
    for kind in kinds:
        blocked = blocked_operations(kind)
        blocked_any = blocked_any or bool(blocked)
        print(f"{kind.name}: {'BLOCKED' if blocked else 'ready'} ({kind.control_root})")
        for work, reason in blocked:
            print(f"  {work.name}: {reason}")
    for kind in kinds:
        entries = quarantine_entries(kind.quarantine_root)
        print(f"quarantine {kind.quarantine_root}: {len(entries)} entries")
        for entry in entries[-5:]:
            print(f"  {entry.name}")
    if blocked_any:
        print("run `ava backup operations retire` to re-prove closure and release a blocked kind")
    return 1 if blocked_any else 0


def cmd_backup_operations_retire(*, confirm: bool) -> int:
    """Re-prove closure of blocked operations; `--confirm` quarantines the proven ones."""
    from base.native_process.os_platform import LockTimeoutError

    refused = found = False
    for kind in _operation_kinds():
        try:
            reports = retire_blocked(kind, confirm=confirm)
        except LockTimeoutError:
            print(f"{kind.name}: an operation is running; retry once it settles", file=sys.stderr)
            refused = True
            continue
        for report in reports:
            found = True
            if report.refusal is not None:
                refused = True
                verdict = "retirement NOT finished" if report.proven else "closure NOT proven"
                print(
                    f"{kind.name} {report.work.name}: {verdict} [{report.refusal}]: {report.reason}",
                    file=sys.stderr,
                )
            elif report.entry is not None:
                print(f"{kind.name} {report.work.name}: retired into {report.entry}")
            else:
                print(f"{kind.name} {report.work.name}: closure proven ({report.reason})")
    if not found and not refused:
        print("no blocked operations")
    elif found and not confirm:
        print("preview only: re-run with --confirm to quarantine every proven operation")
    return 1 if refused else 0
