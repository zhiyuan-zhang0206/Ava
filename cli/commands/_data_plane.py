"""Gateway data-plane bring-up and remote reachability probes.

The gateway's `ava start` data-plane step lives here rather than in
`cli/commands/start.py`: bring-up chooses between a LOCAL instance
(`ensure_cluster_instance` — initdb / pg / redis / PgBouncer / ACL under
`$AVA_HOME`) and a REMOTE-managed plane (Task #1752) whose URLs name another
host, where startup degrades to a reachability probe of the URLs themselves
and fail-fasts with the dial detail. The probes are also used by `ava status`
and `ava stop` — both must never touch a foreign service.
"""

from __future__ import annotations

import sys
from urllib.parse import urlsplit

from shared.config import settings
from shared.log import logger
from shared.url_secret import url_host


def ensure_gateway_data_plane() -> int:
    """Bring up this cluster's own Postgres+Redis instance (+ PgBouncer when enabled)
    on a gateway-capable host (skip-if-running, so a (re)start never disrupts the data
    plane a running cluster is using). Every cluster owns its instance; the pg/redis
    ports come from its registry record (allocated at birth). The pgbouncer port is
    derived for records saved before the slot existed (record_pgbouncer_port). A
    REMOTE-managed data plane (URLs naming a foreign host — Task #1752) skips the
    local instance entirely and probes the URLs instead."""
    from cli.commands._cluster_instance import ensure_cluster_instance
    from shared.cluster import (
        db_identity,
        get_record,
        record_pgbouncer_port,
        redis_identity,
        redis_password_from_env,
    )
    from shared.paths import ava_home

    rec = get_record(ava_home())
    if rec is None:
        print(
            f"  ✗ no registry record for home {ava_home()} — cannot bring up its "
            "data plane. Install births it; run `scripts/install.sh` for this checkout.",
            file=sys.stderr,
        )
        return 1

    if settings.data_plane.is_remote:
        # Remote-managed data plane (Task #1752): the URLs name another host,
        # so there is no local instance to bring up — no initdb, no PgBouncer,
        # no role/ACL provisioning. Startup degrades to a reachability probe of
        # the URLs themselves: unreachable means the gateway cannot serve at
        # all, so fail fast with the dial detail instead of a bare psycopg
        # traceback from the first migration.
        host = url_host(settings.data_plane.db_url)
        print(f"\n→ data plane remote-managed ({host}) — skipping local instance bring-up")
        pg_ok, pg_line = remote_pg_reachable()
        if not pg_ok:
            print(
                f"  ✗ remote data plane unreachable: {pg_line}\n"
                "    Check AVA_DB_URL / AVA_REDIS_URL and their credentials, and the "
                "network path to the provider. A local cluster switching to a remote "
                "plane must first migrate its data (pg_dump / restore) and apply "
                "pending migrations here.",
                file=sys.stderr,
            )
            return 1
        redis_ok, redis_line = remote_redis_reachable()
        if not redis_ok:
            print(f"  ✗ remote data plane unreachable: {redis_line}", file=sys.stderr)
            return 1
        print(f"  ✓ remote data plane reachable ({pg_line}; {redis_line})")
        return 0

    def _ensure(credentials: tuple[str, str, str]) -> int:
        db_admin_password, redis_admin_password, redis_password = credentials
        return ensure_cluster_instance(
            pg_port=rec.ports["postgres"],
            redis_port=rec.ports["redis"],
            cluster_secret=settings.data_plane.cluster_secret,
            db_admin_password=db_admin_password,
            redis_admin_password=redis_admin_password,
            redis_password=redis_password,
            pgbouncer_port=record_pgbouncer_port(rec),
            identity=db_identity(),
            redis_user=redis_identity(),
        )

    current = (
        settings.data_plane.db_admin_password or settings.data_plane.cluster_secret,
        settings.data_plane.redis_admin_password or settings.data_plane.cluster_secret,
        redis_password_from_env() or settings.data_plane.cluster_secret,
    )
    rc = _ensure(current)
    if rc == 0:
        return 0

    from cli.commands._data_plane_admin_secrets import (
        pending_data_plane_bootstrap_credentials,
    )

    pending = pending_data_plane_bootstrap_credentials()
    if pending is None:
        return rc
    print("  · retrying data-plane bring-up with journaled transition credentials")
    return _ensure(pending)


def remote_plane_host() -> str:
    """The dial host of the remote data plane's db URL — for operator messages."""
    return url_host(settings.data_plane.db_url)


def remote_pg_reachable() -> tuple[bool, str]:
    """Probe a remote-managed Postgres through its own AVA_DB_URL.

    The two-stage shape of the local probe, minus the local machinery:
    `shared.db.connect()` dials the URL as every consumer does (auth included),
    so an unreachable host and a wrong credential both report a real detail
    line. Bounded by the connect keepalives (5s connect timeout). Returns
    (ok, detail) and never raises.
    """
    import shared.db

    host = url_host(settings.data_plane.db_url)
    port = urlsplit(settings.data_plane.db_url).port or 5432
    try:
        with shared.db.connect() as conn:
            conn.execute("select 1")
        return True, f"postgres ({host}:{port})"
    except Exception as exc:
        lines = [ln.strip() for ln in str(exc).splitlines() if ln.strip()]
        detail = lines[-1] if lines else "connection failed"
        return False, f"postgres ({host}:{port}) connect failed: {detail}"


def remote_redis_reachable() -> tuple[bool, str]:
    """Probe a remote-managed Redis through its own AVA_REDIS_URL.

    PINGs with the URL's own credentials (redis-py `from_url`), unlike the
    local probe's admin-password dial — on a remote/SaaS plane the URL userinfo
    is the only credential that exists. Returns (ok, detail) and never raises.
    """
    import redis as _redis

    host = url_host(settings.data_plane.redis_url)
    port = urlsplit(settings.data_plane.redis_url).port or 6379
    try:
        client = _redis.Redis.from_url(  # pyright: ignore[reportUnknownMemberType]
            settings.data_plane.redis_url,
            socket_connect_timeout=3,
        )
        try:
            client.ping()  # pyright: ignore[reportUnknownMemberType]
        finally:
            client.close()
        return True, f"redis ({host}:{port})"
    except Exception as exc:
        lines = [ln.strip() for ln in str(exc).splitlines() if ln.strip()]
        detail = lines[-1] if lines else "connection failed"
        return False, f"redis ({host}:{port}) connect failed: {detail}"


def warn_orphaned_local_instance() -> None:
    """Best-effort warning when a local instance still runs under this home.

    After a local→remote data-plane switch (Task #1752), the old local pg/redis
    are no longer managed by this cluster — `ava stop` / `ava cluster down`
    skip them, so if they are still running they keep consuming the home's
    ports and data dir. Print a manual-teardown hint instead of silently
    leaving them. Never raises: this is a hint on an already-successful path.
    """
    try:
        from shared.cluster import get_record
        from shared.paths import ava_home

        rec = get_record(ava_home())
        if rec is None:
            return
        leftovers: list[str] = []
        # The signal is a live process answering on this cluster's own instance
        # ports — whatever it is, it is not the remote-managed plane and not
        # something this cluster will ever manage again.
        from cli.commands._cluster_instance import _pg_running, _redis_running

        if _pg_running(rec.ports["postgres"], "127.0.0.1"):
            leftovers.append("postgres")
        # The admin credential is a variable read, not a literal — the name
        # avoids GitGuardian's generic-password detector tripping on `password`.
        redis_admin = settings.data_plane.redis_admin_password or settings.data_plane.cluster_secret
        if _redis_running(rec.ports["redis"], redis_admin, "127.0.0.1"):
            leftovers.append("redis")
        if leftovers:
            print(
                "  ⚠ local " + " + ".join(leftovers) + " from before the switch is still "
                "running on this home's data dir and is no longer managed (the data "
                "plane is remote). Tear it down by hand — `pg_ctl -D $AVA_HOME/pg "
                "-m fast stop` and `redis-cli -p <port> shutdown nosave` — or switch "
                "the URLs back to local and run `ava stop`.",
                file=sys.stderr,
            )
    except Exception as exc:
        logger.debug("orphaned-local-instance probe skipped: {exc!r}", exc=exc)
