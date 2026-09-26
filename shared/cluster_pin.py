"""Legacy cluster pin row: a historical value that no current lifecycle writes.

`cluster_pin` (one row, central DB) holds the commit the retired in-place
updater last pinned (`target_sha`) and its known-good bookkeeping
(`last_known_good_sha`, `pending_known_good_sha`). Nothing advances these
columns any more: the retained release journal (`cli/release_transition/`) is
the release record, and known-good publication belongs to the release
operation, which has not implemented it yet. The values are therefore frozen at
whatever the legacy updater last wrote, and no operator surface presents them
as the cluster's current target or rollback anchor.

The one remaining reader is `ops.deploy_window.settle_hosts_converged`, which
may release a settle hold the legacy updater left behind (bounded by its TTL).
Removing the row belongs to the cutover's retired-storage cleanup
(`future/infra/unified-cluster-lifecycle.md`).
"""

from __future__ import annotations

import psycopg

import shared.db


def _target_sha_from_connection(conn: psycopg.Connection) -> str | None:
    with conn.cursor() as cur:
        cur.execute("SELECT target_sha FROM cluster_pin WHERE id = 1")
        row = cur.fetchone()
        if row is None:
            raise RuntimeError("cluster_pin singleton row missing (migration 0026 seeds it)")
        return row[0]


def get_cluster_target_sha(*, conn: psycopg.Connection | None = None) -> str | None:
    """The legacy pinned commit, or None when the retired updater never pinned one.

    A supplied connection is reused instead of borrowing a second one.

    None means "the singleton row exists but `target_sha IS NULL`". A *missing*
    row is an invariant breach (migration 0026 seeds it and nothing deletes it),
    so it raises rather than collapsing to the same None."""
    if conn is not None:
        return _target_sha_from_connection(conn)
    with shared.db.connect(autocommit=True) as borrowed:
        return _target_sha_from_connection(borrowed)
