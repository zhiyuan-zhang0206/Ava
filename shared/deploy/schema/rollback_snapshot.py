"""Names reserved for finite migration rollback snapshots."""

from __future__ import annotations

import re

_ROLLBACK_SNAPSHOT_TABLE_RE = re.compile(r"[a-z][a-z0-9_]*_backfill_[a-z0-9_]*")


def is_rollback_snapshot_table(table: str) -> bool:
    """Return whether ``table`` follows the migration rollback-snapshot convention.

    The convention is shared by the migration lint and the archive CLI: a
    ``*_backfill_*`` table contains finite recovery data rather than durable
    application state, so it needs a forward drop plan and an archive before
    retirement.
    """
    return _ROLLBACK_SNAPSHOT_TABLE_RE.fullmatch(table) is not None
