"""This home's PgBouncer pooler: where its files live and whether it answers.

The probes are the observation half of the pooler's lifecycle: the cli owns bring-up and
stop (`cli.commands.data_plane.pgbouncer`), the root diagnostics and the data-plane status
line only look. Nothing here starts, stops, repairs or rewrites the pooler.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from base.cluster import machine, port_preflight
from base.cluster.authority import POOLER_ADMIN
from base.paths import ava_home


def pooler_dir() -> Path:
    d = ava_home() / "pgbouncer"
    d.mkdir(parents=True, exist_ok=True)
    return d


def ini_path() -> Path:
    return pooler_dir() / "pgbouncer.ini"


def pidfile_path() -> Path:
    return pooler_dir() / "pgbouncer.pid"


def admin_reachable(listen_port: int, admin_password: str, host: str = "127.0.0.1") -> bool:
    """Authenticate to the pooler's admin console without opening a backend.

    Backend readiness is proven separately by the caller, as each delivered
    login. Public bind verification reads the socket table, never a self-dial.
    """
    from base.db.connections import connect_url
    from base.host.net.url_secret import url_with_userinfo

    url = url_with_userinfo(
        f"postgresql://@{host}:{listen_port}/pgbouncer", POOLER_ADMIN, admin_password
    )
    try:
        # The console runs no Postgres statements: no ceiling in its startup packet.
        with connect_url(url, autocommit=True, connect_timeout=3, unbounded=True):
            return True
    except Exception:
        return False


def pgbouncer_public_listener_reachable(listen_port: int, role: str, cluster_secret: str) -> bool:
    """True when the pooler listens on the address remote consumers actually dial.

    The loopback probe (`pgbouncer_listener_reachable`) proves "the pooler process
    is there"; this one proves "the PUBLIC front door is open". A pooler whose
    `listen_addr` includes the reachable address but failed to bind it
    keeps running on loopback alone — pgbouncer treats a failed bind as a WARNING,
    not an error — and a loopback-only probe cannot tell the difference, so
    `AVA_DB_URL`'s public path stays silently dead for every enrolled agent-runner
    (task #1288: 2026-08-16 a boot-time address race left the pooler loopback-only
    for two days).

    A local socket-table read is the authoritative fact for this question. A
    network self-dial through the reachable address is a hairpin route that VPN
    filtering can intermittently block even while the listener remains bound;
    treating that routing failure as a missing bind causes destructive false
    restarts. The exact reachable address and IPv4/IPv6 wildcard binds all cover
    the public front door. An empty table proves nothing and remains degraded.

    A no-secret cluster's pooler binds loopback only by design (`bind_addrs`), so
    there is no public listener to check — returns True without inspecting the
    host or socket table. `role` remains in the stable probe signature shared by
    the healthcheck and bring-up callers; socket inspection needs no credential."""
    del role
    if port_preflight.bind_addrs(cluster_secret) == ["127.0.0.1"]:
        return True
    reachable = machine.reachable_host()
    addrs = port_preflight.listener_addrs(listen_port)
    return bool(addrs & {reachable, "0.0.0.0", "::", "*"})  # noqa: S104 — matching OS wildcard binds, not opening one


def pgbouncer_listener_reachable(listen_port: int, admin_password: str) -> bool:
    """Is the POOLER itself up — the admin-console probe, with no server hop.

    The watchdog healthcheck's question: an end-to-end `SELECT 1` also fails when
    Postgres is down, and restarting the pooler is the wrong answer to that. This
    separates "the pooler process is gone" (repairable by `ensure_pgbouncer`) from
    "the pooler is fine and the backend behind it is not"."""
    return admin_reachable(listen_port, admin_password)


@dataclass(frozen=True)
class PoolerClient:
    """One client connection the pooler holds, as its admin console lists it."""

    address: str
    database: str
    user: str
    state: str
    application: str

    def describe(self) -> str:
        app = f" app={self.application}" if self.application else ""
        return f"{self.address} db={self.database} user={self.user} state={self.state}{app}"


def clients(listen_port: int, admin_password: str, host: str = "127.0.0.1") -> list[PoolerClient]:
    """The client connections open on the pooler (`SHOW CLIENTS`), sorted by address.

    The admin console's own connection is left out. Raises when the console cannot be
    read: a caller that reports on behalf of an operator says so rather than showing an
    empty list for a pooler it never looked at.
    """
    from psycopg.rows import dict_row

    from base.db.connections import connect_url
    from base.host.net.url_secret import url_with_userinfo

    url = url_with_userinfo(
        f"postgresql://@{host}:{listen_port}/pgbouncer", POOLER_ADMIN, admin_password
    )
    with (
        connect_url(url, autocommit=True, connect_timeout=3, unbounded=True) as conn,
        conn.cursor(row_factory=dict_row) as cur,
    ):
        cur.execute("SHOW CLIENTS")
        rows = cur.fetchall()
    found = [
        PoolerClient(
            address=f"{row['addr']}:{row['port']}",
            database=str(row["database"]),
            user=str(row["user"]),
            state=str(row["state"]),
            application=str(row.get("application_name") or ""),
        )
        for row in rows
        if row["database"] != "pgbouncer"
    ]
    return sorted(found, key=lambda client: client.address)
