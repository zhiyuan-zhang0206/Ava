"""Cluster Postgres admin-plane dialing — the provisioning admin connection.

Moved down from `cli.commands._cluster_instance` (tech audit 2026-08-31, QA
#1133 P2 observation): the PITR services reach for the admin URL but must not
import up into `cli`. Everything here is `shared`-level (paths / cluster /
private-storage), so the admin dial lives beside the identity it serves.
"""

from __future__ import annotations

import getpass
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import psycopg

from shared.cluster.derive import home_slug
from shared.private_storage import ensure_private_dir


def pg_socket_dir(socket_root: Path | None = None, *, home: Path | None = None) -> Path:
    """A SHORT, cluster-unique socket directory. The Postgres socket path
    (`<dir>/.s.PGSQL.<port>`) is capped at 103 bytes, so it cannot live under a
    deep `$AVA_HOME` / pytest-tmp data dir — a short `/tmp/ava-pg-<home-slug>`
    (keyed on the cluster home path, never a name) stays well under the cap. The
    socket only serves local provisioning (the runtime connects over TCP); 0700
    keeps it owner-only. `home` defaults to `ava_home()` resolved in THIS module;
    the cli thin shell passes its own resolution so cli-layer steering (tests
    patch `cli.commands._cluster_instance.ava_home`) keeps flowing."""
    if home is None:
        from shared.paths import ava_home

        home = ava_home()
    root = Path("/tmp") if socket_root is None else socket_root  # noqa: S108 — OS-fixed production socket root
    d = root / f"ava-pg-{home_slug(home)}"
    return ensure_private_dir(d)


def pg_admin_url(pg_port: int) -> str:
    """Dial only this home's canonical owner-only Unix socket directory."""
    return f"postgresql://{getpass.getuser()}@/postgres?host={pg_socket_dir()}&port={pg_port}"


@contextmanager
def connect(
    url: str, *, expected_data_dir: Path | None = None, **kwargs: Any
) -> Generator[psycopg.Connection[Any]]:
    """Open explicit admin authority; owned startup validates the actual connection.

    A caller managing native storage must supply its recorded data directory.
    Provider-managed provisioning helpers retain their explicit URL authority.
    Psycopg connections never reconnect, so validated custody lasts until close.
    """
    with psycopg.connect(url, **kwargs) as conn:
        if expected_data_dir is not None:
            from shared.cluster.ownership import require_postgres_connection

            require_postgres_connection(conn, expected_data_dir)
        yield conn
