"""Pending-migration applier — a step of `ava start`, not a standalone command.

There is no `ava migrations` CLI surface. `cmd_migrations_apply` is called by
`cli/commands/lifecycle/start.py` early in boot (after pg is up, before the schema-current
assertion); long-running daemons additionally call `assert_schema_current` on
their own entry points.
"""

from __future__ import annotations

from typing import Any

import psycopg


def cmd_migrations_apply() -> list[str]:
    """Apply Ava migrations and verify checkpoint schema; `ava start` step 2.5.

    Ava SQL files run on every host (a runner normally has nothing pending and
    the authority guard refuses it if it does). Every host then verifies the
    complete LangGraph checkpoint migration set read-only. Runtime checkpoint
    readers and agent boot may dial as ``ava_runner`` and never perform DDL.

    Each Ava migration runs in a single transaction. `ava start` invokes this
    after pg is ready; on the gateway the fleet update reaches it through
    the trailing `ava start`.

    Returns the names applied, NOT an exit code — the `cmd_` prefix is
    vestigial here (see the module docstring: there is no `ava migrations`
    verb, and this is never wired to a parser). Its caller needs the set,
    because a migration that created a table is the moment `ava_runner`'s
    point-in-time read grant went stale; failure is raised, not returned.
    """
    from base import cluster
    from base.config import settings
    from base.db import Database, pg_admin

    # Dependency drift is a pre-DB gate: a new upstream checkpoint migration
    # must first be mirrored in an Ava migration. Failing before
    # Ava SQL runs keeps update recovery on the old code + old schema.
    cluster.assert_checkpoint_dependency_pinned()

    # Both dials bypass PgBouncer — the ONE sanctioned data-plane exemption
    # (user ruling 2026-08: every consumer goes through PgBouncer; see
    # base/db/__init__.py `connect`). apply_pending_migrations holds a SESSION
    # advisory lock (pg_advisory_lock, base/deploy/schema/migrations.py _MIGRATION_LOCK_KEY)
    # across its whole apply loop, and transaction pooling hands the backend
    # back to the pool at the end of each transaction — the lock would silently
    # drop between statements, letting a concurrent applier interleave DDL.
    # Both are also unbounded: migration DDL may legitimately exceed the 60s
    # statement ceiling (large-table rebuilds, partition backfills).
    if settings.data_plane.is_remote:
        # A remote-managed plane's provider URL is its only authority.
        with Database.from_settings().connect(direct=True, unbounded=True) as conn:
            done = _apply(conn)
        cluster.assert_checkpoint_schema_current(Database.from_settings().direct_url())
    else:
        # A locally owned plane migrates as the administrator acting as the
        # schema owner over the home's own socket: objects stay owner-owned and
        # the owner's own login is never used.
        authority = pg_admin.local_owner_authority()
        with authority.session() as conn:
            done = _apply(conn)
        cluster.assert_checkpoint_schema_current(
            authority.conninfo, expected_data_dir=authority.data_dir
        )
    print(f"applied {len(done)} migration(s): {done}")
    return done


def _apply(conn: psycopg.Connection[Any]) -> list[str]:
    from base.deploy.schema.migrations import apply_pending_migrations

    return apply_pending_migrations(conn)
