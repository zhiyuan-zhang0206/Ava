"""Cluster Postgres admin-plane dialing — the provisioning admin connection.

Moved down from `cli.commands._cluster_instance` (tech audit 2026-08-31, QA
#1133 P2 observation): the PITR services reach for the admin URL but must not
import up into `cli`. Everything here is `shared`-level (paths / cluster /
private-storage), so the admin dial lives beside the identity it serves.
"""

from __future__ import annotations

import getpass
from pathlib import Path

from shared.cluster import home_slug
from shared.paths import ava_home
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
        home = ava_home()
    root = Path("/tmp") if socket_root is None else socket_root  # noqa: S108 — OS-fixed production socket root
    d = root / f"ava-pg-{home_slug(home)}"
    return ensure_private_dir(d)


def live_pg_socket_dir(
    pg_port: int,
    probe_root: Path = Path("/tmp"),  # noqa: S108 — the OS-fixed short socket root
    *,
    canonical: Path | None = None,
) -> Path:
    """The socket dir the RUNNING pg instance on `pg_port` actually listens on.

    Normally the canonical `pg_socket_dir()`. A pg started by pre-path-only code
    still listens under the old name-keyed `/tmp/ava-pg-<cluster>` until its next
    restart, so the admin dial probes every `<probe_root>/ava-pg-*` dir for a live
    `.s.PGSQL.<pg_port>` socket — the port is this cluster's own allocated one,
    so a match is unambiguous (data, not a name). `canonical` overrides the
    canonical-dir source (the cli thin shell binds its monkeypatchable
    `_pg_socket_dir` through it). Falls back to the canonical dir when nothing
    is listening yet (fresh birth: the socket appears when `_start_pg` starts
    pg there). `probe_root` is /tmp in production (where the short socket dirs
    live); tests inject a scratch root."""
    if canonical is None:
        canonical = pg_socket_dir()
    if (canonical / f".s.PGSQL.{pg_port}").exists():
        return canonical
    for d in probe_root.glob("ava-pg-*"):
        if (d / f".s.PGSQL.{pg_port}").exists():
            return d
    return canonical


def pg_admin_url(pg_port: int) -> str:
    """The provisioning admin connection for this cluster's instance: the initdb
    superuser over the local unix socket (trust), so provisioning is passwordless.
    psycopg reads `host=<socket-dir>` + `port` from the query string. Dials the
    socket dir the running instance actually listens on (`live_pg_socket_dir`),
    so an admin call keeps working while a pre-cutover pg is still up on the old
    name-keyed dir."""
    return (
        f"postgresql://{getpass.getuser()}@/postgres"
        f"?host={live_pg_socket_dir(pg_port)}&port={pg_port}"
    )
