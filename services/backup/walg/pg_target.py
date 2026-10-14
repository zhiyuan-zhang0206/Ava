"""Where this home's Postgres is, for the tick and the recovery drill.

The owner-only admin socket, the data directory that proves a connection reaches this
home's postmaster, and the application database name (read as data from the cluster's URL).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from base.cluster.dataplane import pg_admin


@dataclass(frozen=True)
class PgTarget:
    """This home's Postgres: its owner-only admin socket, data directory and database."""

    admin_url: str
    data_dir: Path
    database: str


def pg_target() -> PgTarget:
    """Resolve this home's locally owned Postgres.

    Raises:
        RuntimeError: the data plane is remote-managed or has no registry record.
    """
    from base.config import settings

    data_plane = settings.data_plane  # ambient: S
    authority = pg_admin.local_owner_authority(data_plane.db_url, remote_plane=data_plane.is_remote)
    return PgTarget(
        admin_url=authority.admin_url, data_dir=authority.data_dir, database=authority.database
    )
