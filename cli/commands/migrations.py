"""Pending-migration applier — a step of `ava start`, not a standalone command.

There is no `ava migrations` CLI surface. `cmd_migrations_apply` is called by
`cli/commands/start.py` early in boot (after pg is up, before the schema-current
assertion); long-running daemons additionally call `assert_schema_current` on
their own entry points.
"""

from __future__ import annotations


def cmd_migrations_apply() -> list[str]:
    """Apply Ava + LangGraph checkpoint migrations; step 2.5 of `ava start`.

    Ava SQL files run on every host (a runner normally has nothing pending and
    the authority guard refuses it if it does). LangGraph's
    ``PostgresSaver.setup()`` runs only on the gateway, under the same schema
    lock and authority check. Runtime checkpoint readers must never perform
    this DDL: they may dial as the least-privilege ``ava_runner`` role.

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
    from shared.machine import is_gateway
    from shared.migrations import (
        apply_pending_migrations,
        assert_migration_authority,
        schema_mutation_lock,
    )

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
        if is_gateway():
            # One lock spans both schema domains. apply_pending_migrations()
            # re-acquires it on this same session (Postgres advisory locks are
            # reentrant); keeping the outer acquisition held serializes the
            # second, autocommit connection LangGraph needs for CREATE INDEX
            # CONCURRENTLY. Authority is explicit even when Ava has no pending
            # SQL files because setup() itself is still a schema mutation path.
            with schema_mutation_lock(conn):
                assert_migration_authority(conn)
                done = apply_pending_migrations(conn)
                checkpoint_versions = cluster.migrate_checkpoint_schema(shared.db.direct_db_url())
        else:
            done = apply_pending_migrations(conn)
            checkpoint_versions = []
    done.extend(f"langgraph-checkpoint-v{version}" for version in checkpoint_versions)
    print(f"applied {len(done)} migration(s): {done}")
    return done
