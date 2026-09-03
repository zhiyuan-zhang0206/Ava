"""Pending-migration applier — a step of `ava start`, not a standalone command.

There is no `ava migrations` CLI surface. `cmd_migrations_apply` is called by
`cli/commands/start.py` early in boot (after pg is up, before the schema-current
assertion); long-running daemons additionally call `assert_schema_current` on
their own entry points.
"""

from __future__ import annotations

from shared.runtime_migration import ReleaseMigrationContext


def cmd_migrations_apply(*, release: ReleaseMigrationContext | None = None) -> list[str]:
    """Apply Ava migrations and verify checkpoint schema; `ava start` step 2.5.

    Ava SQL files run on every host (a runner normally has nothing pending and
    the authority guard refuses it if it does). Every host then verifies the
    complete LangGraph checkpoint migration set read-only. Runtime checkpoint
    readers and agent boot may dial as ``ava_runner`` and never perform DDL.

    Each Ava migration runs in a single transaction. `ava start` invokes this
    after pg is ready; on the gateway `ava cluster update` reaches it through
    the trailing `ava start`.

    Returns the names applied, NOT an exit code — the `cmd_` prefix is
    vestigial here (see the module docstring: there is no `ava migrations`
    verb, and this is never wired to a parser). Its caller needs the set,
    because a migration that created a table is the moment `ava_runner`'s
    point-in-time read grant went stale; failure is raised, not returned.
    """
    import shared.db
    from shared import cluster
    from shared.migrations import apply_pending_migrations

    # Dependency drift is a pre-DB gate: a new upstream checkpoint migration
    # must first be mirrored in a paired Ava up/down migration. Failing before
    # Ava SQL runs keeps update recovery on the old code + old schema.
    cluster.assert_checkpoint_dependency_pinned()

    # direct=True — the ONE sanctioned data-plane exemption (user ruling 2026-08:
    # every consumer goes through PgBouncer; see shared/db.py `connect`).
    # apply_pending_migrations holds a SESSION advisory lock (pg_advisory_lock,
    # shared/migrations.py _MIGRATION_LOCK_KEY) across its whole apply loop, and
    # transaction pooling hands the backend back to the pool at the end of each
    # transaction — the lock would silently drop between statements, letting a
    # concurrent applier interleave DDL. Migrations bypassing the pooler is the
    # industry convention (PgBouncer docs: admin/DDL work must not run through a
    # transaction pooler; every ORM/framework does the same).
    # unbounded=True — migration DDL may legitimately exceed the 60s statement
    # ceiling (large-table rebuilds, partition backfills); the applier must
    # stay unbounded (shared/db.py PG_STATEMENT_TIMEOUT_*).
    with shared.db.connect(direct=True, unbounded=True) as conn:
        if release is None:
            done = apply_pending_migrations(conn)
        else:
            done = apply_pending_migrations(conn, release=release)
    cluster.assert_checkpoint_schema_current(shared.db.direct_db_url())
    print(f"applied {len(done)} migration(s): {done}")
    return done
