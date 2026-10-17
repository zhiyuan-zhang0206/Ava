"""Pending-migration applier — a step of `meiva start`, not a standalone command.

There is no `meiva migrations` CLI surface. `cmd_migrations_apply` is called by
`cli/commands/lifecycle/start.py` early in boot (after pg is up, before the schema-current
assertion); long-running daemons additionally call `assert_schema_current` on
their own entry points.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import psycopg

from cli.database import OperatorDatabaseFactory


def cmd_migrations_apply(
    *, database_factory: OperatorDatabaseFactory, machine_name: Callable[[], str]
) -> list[str]:
    """Apply Meiva migrations and verify checkpoint schema; `meiva start` step 2.5.

    Meiva SQL files run on every host (a runner normally has nothing pending and
    the authority guard refuses it if it does). Every host then verifies the
    complete LangGraph checkpoint migration set read-only. Runtime checkpoint
    readers and agent boot may dial as ``meiva_runner`` and never perform DDL.

    Each Meiva migration runs in a single transaction. `meiva start` invokes this
    after pg is ready; on the gateway the fleet update reaches it through
    the trailing `meiva start`.

    Returns the names applied, NOT an exit code — the `cmd_` prefix is
    vestigial here (see the module docstring: there is no `meiva migrations`
    verb, and this is never wired to a parser). Its caller needs the set,
    because a migration that created a table is the moment `meiva_runner`'s
    point-in-time read grant went stale; failure is raised, not returned.
    """
    from base import cluster
    from base.cluster.dataplane import pg_admin
    from base.config import settings
    from base.deploy.schema.migration_layout import required_migration_set

    # Dependency drift is a pre-DB gate: a new upstream checkpoint migration
    # must first be mirrored in an Meiva migration. Failing before
    # Meiva SQL runs keeps update recovery on the old code + old schema.
    cluster.assert_checkpoint_dependency_pinned(required_migration_set())

    if settings.data_plane.is_remote:
        with database_factory().connect(unbounded=True) as conn:
            done = _apply(conn, machine_name=machine_name)
        cluster.assert_checkpoint_schema_current(database_factory.db_url())
    else:
        # A locally owned plane migrates as the administrator acting as the
        # schema owner over the home's own socket: objects stay owner-owned and
        # the owner's own login is never used.
        authority = pg_admin.local_owner_authority(settings.data_plane.db_url, remote_plane=False)
        with authority.session() as conn:
            done = _apply(conn, machine_name=machine_name)
        cluster.assert_checkpoint_schema_current(
            authority.conninfo, expected_data_dir=authority.data_dir
        )
    print(f"applied {len(done)} migration(s): {done}")
    return done


def _apply(conn: psycopg.Connection[Any], *, machine_name: Callable[[], str]) -> list[str]:
    from base.deploy.schema.migrations import apply_pending_migrations

    return apply_pending_migrations(conn, machine_name=machine_name)
