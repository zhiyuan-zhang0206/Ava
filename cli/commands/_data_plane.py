"""Gateway data-plane bring-up and remote reachability probes.

The gateway's `ava start` data-plane step lives here rather than in
`cli/commands/start.py`: bring-up chooses between a LOCAL instance
(`ensure_cluster_storage` — initdb / pg / redis / ACL under
`$AVA_HOME`) and a REMOTE-managed plane (Task #1752) whose URLs name another
host, where startup degrades to a reachability probe of the URLs themselves
and fail-fasts with the dial detail. The probes are also used by `ava status`
and `ava stop` — both must never touch a foreign service.
"""

from __future__ import annotations

import socket
import sys
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import psycopg

from ops.service_spec import DbAccess
from shared.cluster.authority.model import Generation
from shared.cluster.registry import ClusterRecord
from shared.config import settings
from shared.log import logger
from shared.url_secret import url_host


def ensure_gateway_data_plane() -> int:
    """Bring up owned storage, or probe an explicitly remote-managed plane.

    Pooler startup belongs to ``complete_gateway_data_plane``, after schema and
    runner grants. Storage ports always come from this home's reservation.
    """
    from cli.commands._cluster_instance import ensure_cluster_storage
    from shared.cluster import (
        get_record,
        redis_identity,
        redis_password_from_env,
    )
    from shared.paths import ava_home

    rec = get_record(ava_home())
    if rec is None:
        print(
            f"  ✗ no registry record for home {ava_home()} — cannot bring up its "
            "data plane. Run this home's `ava start` with its first-start identity.",
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

    from cli.start_identity import needs_provision
    from shared.cluster.authority import load_ledger

    if not needs_provision(ava_home()) and load_ledger(ava_home().resolve()) is None:
        # A home born before the data plane always authenticated: refuse before
        # any native effect (hba rewrite, migrations). Never converted here.
        print(f"  ✗ {cutover_instruction(ava_home().resolve())}", file=sys.stderr)
        return 1
    return ensure_cluster_storage(
        pg_port=rec.ports["postgres"],
        redis_port=rec.ports["redis"],
        cluster_secret=settings.data_plane.cluster_secret,
        redis_admin_password=settings.data_plane.redis_admin_password,
        redis_password=redis_password_from_env(),
        redis_user=redis_identity(),
    )


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


def _local_listener(port: int) -> bool:
    """Whether anything accepts TCP connections on loopback `port`."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


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
        from cli.commands._cluster_instance import _pg_running

        if _pg_running(rec.ports["postgres"], "127.0.0.1"):
            leftovers.append("postgres")
        # A plain listener check: the left-behind Redis always requires its admin
        # password, which a remote-managed home no longer carries.
        if _local_listener(rec.ports["redis"]):
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


def prepare_gateway_schema() -> None:
    """Initialize only a journal-owned fresh database, before any pooled login."""
    from cli.commands._cluster_instance import pg_admin_url
    from cli.start_identity import needs_provision
    from shared import cluster
    from shared.paths import ava_home

    if settings.data_plane.is_remote or not needs_provision(ava_home()):
        return
    rec = cluster.get_record(ava_home())
    if rec is None:
        raise RuntimeError("initialization lost its port reservation")
    admin_url = pg_admin_url(rec.ports["postgres"])
    identity = cluster.db_identity()
    created = cluster.provision_database(
        identity,
        base_admin_url=admin_url,
        resume_initialization=True,
        expected_data_dir=ava_home() / "pg",
    )
    cluster.ensure_pgvector_extension(
        identity, base_admin_url=admin_url, expected_data_dir=ava_home() / "pg"
    )
    cluster.ensure_checkpoint_schema(
        identity,
        base_admin_url=admin_url,
        database_created=created,
        resume_partial=True,
        expected_data_dir=ava_home() / "pg",
    )


def prepare_memory_vectors() -> None:
    """Create or rebuild the pgvector memory table when that backend is selected.

    The table is a derived cache keyed to the embedding provider's dimension.
    Runtime connections only validate it; this start step is its one writer.
    A local plane writes it acting as the schema owner, before the runner grants
    refresh; a remote-managed plane uses its provider URL, like its migrations.
    """
    if settings.services.memory_search_backend != "pgvector":
        return
    import shared.db
    from services.memory_indexer.backends.pgvector import prepare_table
    from services.memory_indexer.embeddings.factory import get_provider
    from shared.pg_admin import local_owner_authority

    dim = get_provider().dim
    if settings.data_plane.is_remote:
        with shared.db.connect(direct=True) as conn:
            prepare_table(conn, dim)
        return
    with local_owner_authority().session() as conn:
        prepare_table(conn, dim)


# Operator read-only roles the authority invariant admits as grantees
# (SELECT/USAGE/CONNECT only; never group members). Names are data: a home
# without such a role is unaffected.
READONLY_GRANTEES = ("grafana_ro",)


def cutover_instruction(home: Path) -> str:
    """The one explicit conversion an ordinary start names for a legacy home."""
    return (
        f"home {home} has no database authority ledger (born before the data plane "
        "always authenticated). Convert it once with its application stopped: "
        "`ava stop --keep-infra`, then "
        f"`.venv/bin/python scripts/cutover_db_authority.py --home {home} --execute` "
        "(dry-run without --execute), then `ava start`."
    )


@contextmanager
def admin_session(rec: ClusterRecord, database: str) -> Generator[psycopg.Connection[Any]]:
    """The OS-user administrator on this home's owner-only socket (`peer`),
    autocommit, custody-checked against the home's own postmaster."""
    from psycopg.conninfo import make_conninfo

    from cli.commands._cluster_instance import pg_admin_url
    from shared import pg_admin
    from shared.cluster import record_postgres_port
    from shared.paths import ava_home

    url = make_conninfo(pg_admin_url(record_postgres_port(rec)), dbname=database)
    with pg_admin.connect(url, expected_data_dir=ava_home() / "pg", autocommit=True) as conn:
        yield conn


def db_endpoint() -> str:
    """The home's credential-free database endpoint, as its `.env` records it."""
    from dotenv import dotenv_values

    from shared.paths import ava_home

    endpoint = dotenv_values(ava_home() / ".env").get("AVA_DB_URL")
    if not endpoint:
        raise RuntimeError("AVA_DB_URL is missing from the gateway configuration")
    return endpoint


def db_delivery(cls: DbAccess) -> dict[str, str]:
    """The launch-environment database delivery for one service of class `cls`.

    A local plane delivers the active write generation's `cls` login on the
    home's credential-free endpoint plus its non-secret generation number; a
    missing ledger or active generation fails the launch (no fallback). A pure
    runner or a remote-managed plane delivers nothing here: its processes keep
    their bootstrap / provider projection.
    """
    from shared.bootstrap import config_source_is_local
    from shared.cluster.authority import GENERATION_ENV, write_grant
    from shared.paths import ava_home

    if settings.data_plane.is_remote or not config_source_is_local():
        return {}
    grant = write_grant(ava_home().resolve(), cls)
    return {"AVA_DB_URL": grant.dsn(db_endpoint()), GENERATION_ENV: str(grant.number)}


def _ensure_pooler(rec: ClusterRecord, database: str, home: Path, generation: Generation) -> None:
    """Serve exactly `generation` through the owned pooler (restart on change)."""
    if not settings.data_plane.pgbouncer_enabled:
        return
    from cli.commands._pgbouncer import ensure_pgbouncer
    from shared.cluster import record_pgbouncer_port, record_postgres_port
    from shared.cluster.authority import read_pooler_admin, render_userlist

    rc = ensure_pgbouncer(
        pg_port=record_postgres_port(rec),
        listen_port=record_pgbouncer_port(rec),
        db_name=database,
        cluster_secret=settings.data_plane.cluster_secret,
        userlist=render_userlist(home, generation),
        admin_password=read_pooler_admin(home).password,
    )
    if rc:
        raise RuntimeError("pooler did not become ready")


def prove_generation_logins(home: Path, generation: Generation, endpoint: str) -> None:
    """Both logins of `generation` answer `SELECT 1` through the consumer endpoint
    (the pooler when enabled: SCRAM client auth plus the pass-through hop)."""
    from cli.commands._health_preflight import probe_postgres
    from shared.cluster.authority import read_secret
    from shared.url_secret import url_with_userinfo

    secret = read_secret(home, generation)
    for role in (secret.roles.gateway, secret.roles.runner):
        if error := probe_postgres(url_with_userinfo(endpoint, role.name, role.password)):
            raise RuntimeError(f"write generation {generation.number} login {role.name}: {error}")


def adopt_gateway_login(home: Path, endpoint: str) -> None:
    """Point this operator process at the home's active gateway login.

    A start that births (or first admits) the generation has no delivery from
    its own boot, which ran before the ledger existed."""
    import os

    from shared.cluster.authority import GENERATION_ENV, write_grant

    grant = write_grant(home, "gateway")
    os.environ["AVA_DB_URL"] = grant.dsn(endpoint)
    os.environ[GENERATION_ENV] = str(grant.number)
    settings.data_plane.db_url = os.environ["AVA_DB_URL"]


def _birth_generation(conn: psycopg.Connection[Any], home: Path, database: str) -> Generation:
    """Initialization authority: groups, monitor, legacy logins retired, ledger,
    generation 0."""
    from functools import partial

    from shared import cluster
    from shared.cluster import authority

    owner = cluster.db_identity()
    groups = authority.Groups(gateway=authority.GATEWAY_GROUP, runner=authority.RUNNER_GROUP)
    birth = authority.BirthAuthority()
    authority.ensure_groups(conn, owner=owner, database=database, groups=groups)
    authority.ensure_monitor(conn, database=database)
    authority.retire_legacy_logins(conn, owner=owner, groups=groups, authority=birth)
    authority.create_ledger(home, owner=owner, groups=groups, authority=birth)
    authority.ensure_pooler_admin(home, encrypt=partial(authority.scram_verifier, conn))
    authority.mint_generation(conn, home, birth)
    generation = authority.require_ledger(home).unrevoked
    if generation is None:
        raise RuntimeError("birth minted no write generation")
    return generation


def _admitted_generation(
    conn: psycopg.Connection[Any], home: Path, database: str, *, refresh: bool
) -> Generation:
    """Ordinary start: re-grant after migrations, converge the monitor, sweep,
    then the invariant holds."""
    from shared import cluster
    from shared.cluster import authority

    ledger = authority.load_ledger(home)
    if ledger is None:
        raise RuntimeError(cutover_instruction(home))
    if ledger.owner != cluster.db_identity():
        raise RuntimeError(
            f"the database authority ledger records owner {ledger.owner!r}, but AVA_DB_URL "
            f"names database {cluster.db_identity()!r}"
        )
    if ledger.active is None:
        raise RuntimeError(
            "the database authority has no active generation; the finite operation that "
            "revoked or is minting it must continue before an ordinary start"
        )
    if refresh:
        authority.ensure_groups(conn, owner=ledger.owner, database=database, groups=ledger.groups)
        authority.ensure_monitor(conn, database=database)
    authority.sweep(conn, home)
    authority.check_invariant(conn, home, database=database, readonly_grantees=READONLY_GRANTEES)
    return ledger.active


def complete_gateway_data_plane(*, refresh_schema: bool = True) -> None:
    """Grant the final schema, establish the write generation, start the pooler,
    then verify consumer credentials.

    Local plane, first start (initialization authority, `needs_provision`):
    groups -> retire legacy logins -> ledger -> mint generation 0 -> pooler
    serving exactly that pair, proven by a pooled login of each -> activate.
    Every step is idempotent, so an interrupted birth retries to the same
    generation. Ordinary start: groups re-granted after migrations, the NOLOGIN
    sweep, then the fail-closed catalog invariant; the pooler serves the active
    pair (restarted only when its bytes change). A home without a ledger is a
    legacy home and refuses with the cutover instruction — never converted here.
    """
    from cli.commands._health_preflight import probe_postgres, probe_redis
    from cli.start_identity import mark_phase, needs_provision
    from shared import cluster
    from shared.paths import ava_home

    if settings.data_plane.is_remote:
        if refresh_schema:
            prepare_memory_vectors()
        from shared.cluster.derive import runner_db_url_projection

        urls = [settings.data_plane.db_url, runner_db_url_projection(settings.data_plane.db_url)]
        for url in urls:
            if error := probe_postgres(url):
                raise RuntimeError(f"consumer database readiness failed: {error}")
    else:
        from shared.cluster import authority

        home = ava_home().resolve()
        rec = cluster.get_record(ava_home())
        if rec is None:
            raise RuntimeError("start lost its port reservation")
        database = cluster.db_identity()
        endpoint = db_endpoint()
        if refresh_schema:
            from cli.commands._cluster_instance import pg_admin_url

            cluster.ensure_pgvector_extension(
                database,
                base_admin_url=pg_admin_url(rec.ports["postgres"]),
                expected_data_dir=ava_home() / "pg",
            )
            prepare_memory_vectors()
        birth = needs_provision(ava_home())
        if not birth and authority.load_ledger(home) is None:
            raise RuntimeError(cutover_instruction(home))
        with admin_session(rec, database) as conn:
            if birth:
                generation = _birth_generation(conn, home, database)
            else:
                generation = _admitted_generation(conn, home, database, refresh=refresh_schema)
            _ensure_pooler(rec, database, home, generation)
            prove_generation_logins(home, generation, endpoint)
            if authority.require_ledger(home).active is None:
                verified = authority.verify_generation(conn, home)
                authority.activate(home, authority.BirthAuthority(), verified)
        adopt_gateway_login(home, endpoint)
        print(f"  ✓ database write generation {generation.number} admitted")
    if error := probe_redis(settings.data_plane.redis_url):
        raise RuntimeError(f"consumer Redis readiness failed: {error}")
    mark_phase(ava_home(), "provisioned")
