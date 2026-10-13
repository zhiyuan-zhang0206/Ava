"""Gateway data-plane bring-up and remote reachability probes.

The gateway's `ava start` data-plane step lives here rather than in
`cli/commands/lifecycle/start.py`: bring-up chooses between a LOCAL instance
(`ensure_cluster_storage` — initdb / pg / redis / ACL under
`$AVA_HOME`) and a REMOTE-managed plane (Task #1752) whose URLs name another
host, where startup degrades to a reachability probe of the URLs themselves
and fail-fasts with the dial detail. The probes are also used by `ava status`
and `ava stop` — both must never touch a foreign service.
"""

from __future__ import annotations

import socket
import subprocess
import sys
from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import psycopg

from base.agents.context.clients import DatabaseFactory
from base.cluster import ownership
from base.cluster import postgres as owned_postgres
from base.cluster.authority.model import Generation
from base.cluster.dataplane import pooler
from base.cluster.machine import MachineIdentity
from base.cluster.record import ClusterRecord
from base.config import settings
from base.host.net.url_secret import url_host
from base.log import logger
from base.paths import ava_home
from base.telemetry import EventPipeline
from cli.commands.data_plane import cluster_instance as instance
from cli.commands.data_plane import pgbouncer
from services.supervision.roster.service_spec import DbAccess


def ensure_gateway_data_plane(
    *,
    retained_children: list[subprocess.Popen[bytes]] | None = None,
    database_factory: DatabaseFactory,
    machine: MachineIdentity,
) -> int:
    """Bring up owned storage, or probe an explicitly remote-managed plane.

    Pooler startup belongs to ``complete_gateway_data_plane``, after schema and
    runner grants. Storage ports always come from this home's reservation.
    """
    if retained_children is None and not settings.data_plane.is_remote:
        raise ValueError("PostgreSQL launch requires its caller-owned child retention")
    from base.cluster import (
        get_record,
        redis_identity,
        redis_password_from_env,
    )
    from base.cluster.authority.api import publish_telemetry_token

    rec = get_record(ava_home())
    if rec is None:
        print(
            f"  ✗ no registry record for home {ava_home()} — cannot bring up its "
            "data plane. Run `ava init` for this home first.",
            file=sys.stderr,
        )
        return 1

    # The root holding the human secret derives the telemetry token and keeps it where the
    # gateway-side services that probe the observability station can read it.
    publish_telemetry_token(ava_home().resolve(), settings.data_plane.cluster_secret)

    if settings.data_plane.is_remote:
        # Remote-managed data plane (Task #1752): the URLs name another host,
        # so there is no local instance to bring up — no initdb, no PgBouncer,
        # no role/ACL provisioning. Startup degrades to a reachability probe of
        # the URLs themselves: unreachable means the gateway cannot serve at
        # all, so fail fast with the dial detail instead of a bare psycopg
        # traceback from the first migration.
        host = url_host(settings.data_plane.db_url)
        print(f"\n→ data plane remote-managed ({host}) — skipping local instance bring-up")
        pg_ok, pg_line = remote_pg_reachable(database_factory=database_factory)
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

    from base.cluster.authority import load_ledger
    from cli.start_identity import needs_provision

    if not needs_provision(ava_home()) and load_ledger(ava_home().resolve()) is None:
        # A home born before the data plane always authenticated: refuse before
        # any native effect (hba rewrite, migrations). Never converted here.
        print(f"  ✗ {legacy_home_refusal(ava_home().resolve())}", file=sys.stderr)
        return 1
    return instance.ensure_cluster_storage(
        pg_port=rec.ports["postgres"],
        redis_port=rec.ports["redis"],
        redis_admin_password=settings.data_plane.redis_admin_password,
        redis_password=redis_password_from_env(),
        redis_user=redis_identity(),
        retained_children=retained_children,
        machine=machine,
    )


def remote_plane_host() -> str:
    """The dial host of the remote data plane's db URL — for operator messages."""
    return url_host(settings.data_plane.db_url)


def remote_pg_reachable(*, database_factory: DatabaseFactory) -> tuple[bool, str]:
    """Probe a remote-managed Postgres through its own AVA_DB_URL.

    The two-stage shape of the local probe, minus the local machinery:
    `base.db.connect()` dials the URL as every consumer does (auth included),
    so an unreachable host and a wrong credential both report a real detail
    line. Bounded by the connect keepalives (5s connect timeout). Returns
    (ok, detail) and never raises.
    """

    host = url_host(settings.data_plane.db_url)
    port = urlsplit(settings.data_plane.db_url).port or 5432
    try:
        with database_factory().connect() as conn:
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
    are no longer managed by this cluster — `ava stop` skips them, so if they are still running they keep consuming the home's
    ports and data dir. Print a manual-teardown hint instead of silently
    leaving them. Never raises: this is a hint on an already-successful path.
    """
    try:
        from base.cluster import get_record

        rec = get_record(ava_home())
        if rec is None:
            return
        leftovers: list[str] = []
        # The signal is a live process answering on this cluster's own instance
        # ports — whatever it is, it is not the remote-managed plane and not
        # something this cluster will ever manage again.
        if instance.pg_running(rec.ports["postgres"], "127.0.0.1"):
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
    except Exception:
        logger.opt(exception=True).warning(
            "orphaned-local-instance probe failed; no teardown hint was printed"
        )


def prepare_gateway_schema() -> None:
    """Initialize only a journal-owned fresh database, before any pooled login."""
    from base import cluster
    from cli.start_identity import needs_provision

    if settings.data_plane.is_remote or not needs_provision(ava_home()):
        return
    rec = cluster.get_record(ava_home())
    if rec is None:
        raise RuntimeError("initialization lost its port reservation")
    admin_url = instance.pg_admin_url(rec.ports["postgres"])
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


def prepare_memory_vectors(*, database_factory: DatabaseFactory) -> None:
    """Create or rebuild the pgvector memory table when that backend is selected.

    The table is a derived cache keyed to the embedding provider's dimension.
    Runtime connections only validate it; this start step is its one writer.
    A local plane writes it acting as the schema owner, before the runner grants
    refresh; a remote-managed plane uses its provider URL, like its migrations.
    """

    if settings.services.memory_search_backend != "pgvector":
        return
    from base.cluster.dataplane.pg_admin import local_owner_authority
    from services.derived.memory_indexer.backends.pgvector import prepare_table
    from services.derived.memory_indexer.embeddings.factory import get_descriptor

    dim = get_descriptor(settings.services.embedding_backend).dim
    if settings.data_plane.is_remote:
        with database_factory().connect(direct=True) as conn:
            prepare_table(conn, dim)
        return
    with local_owner_authority().session() as conn:
        prepare_table(conn, dim)


# Operator read-only roles the authority invariant admits as grantees
# (SELECT/USAGE/CONNECT only; never group members). Names are data: a home
# without such a role is unaffected.
READONLY_GRANTEES = ("grafana_ro",)


def legacy_home_refusal(home: Path) -> str:
    """Why an ordinary start refuses a home born before the data plane always
    authenticated: nothing converts it."""
    return (
        f"home {home} has no database authority ledger (born before the data plane "
        "always authenticated) and no conversion exists; re-birth it as a new home"
    )


@contextmanager
def admin_session(rec: ClusterRecord, database: str) -> Generator[psycopg.Connection[Any]]:
    """The OS-user administrator on this home's owner-only socket (`peer`),
    autocommit, custody-checked against the home's own postmaster."""
    from psycopg.conninfo import make_conninfo

    from base.cluster.dataplane import pg_admin

    url = make_conninfo(instance.pg_admin_url(rec.ports["postgres"]), dbname=database)
    with pg_admin.connect(url, expected_data_dir=ava_home() / "pg", autocommit=True) as conn:
        yield conn


def db_endpoint() -> str:
    """The home's credential-free database endpoint, as its `.env` records it."""
    from dotenv import dotenv_values

    endpoint = dotenv_values(ava_home() / ".env").get("AVA_DB_URL")
    if not endpoint:
        raise RuntimeError("AVA_DB_URL is missing from the gateway configuration")
    return endpoint


def db_delivery(cls: DbAccess) -> dict[str, str]:
    """The launch-environment database delivery for one service of class `cls`.

    A local plane delivers the active write generation's `cls` login on the
    home's credential-free endpoint plus its non-secret generation number; a
    missing ledger or active generation fails the launch (no fallback). A pure
    agent-runner delivers its installed unit capability (runner class only);
    without one the launch fails with the issue-unit instruction. A
    remote-managed gateway plane delivers nothing here: its agents keep the
    provider projection.
    """
    from base.cluster.authority import GENERATION_ENV, write_grant
    from base.host.env.bootstrap import config_source_is_local

    if not config_source_is_local():
        from base.cluster.authority.unit import unit_delivery

        if cls != "runner":
            raise RuntimeError(f"a pure agent-runner cannot launch a {cls}-class database service")
        return unit_delivery(ava_home().resolve())
    if settings.data_plane.is_remote:
        return {}
    grant = write_grant(ava_home().resolve(), cls)
    return {"AVA_DB_URL": grant.dsn(db_endpoint()), GENERATION_ENV: str(grant.number)}


def api_delivery(cls: DbAccess) -> dict[str, str]:
    """The launch-environment machine API token for one service of class `cls`.

    The cluster's API always authenticates, so every service is delivered a
    token to present as its bearer: a pure agent-runner delivers its
    capability's runner token; the gateway home delivers the active generation's
    `cls` token. A remote-managed plane keeps no write generations and delivers
    nothing; its gateway-local services present the human secret.
    """
    from base.cluster.authority.api import API_TOKEN_ENV, api_token
    from base.host.env.bootstrap import config_source_is_local

    if not config_source_is_local():
        from base.cluster.authority.unit import unit_api_delivery

        if cls != "runner":
            raise RuntimeError(f"a pure agent-runner cannot launch a {cls}-class service")
        return unit_api_delivery(ava_home().resolve())
    if settings.data_plane.is_remote:
        return {}
    return {API_TOKEN_ENV: api_token(ava_home().resolve(), cls)}


def _ensure_pooler(
    rec: ClusterRecord,
    database: str,
    home: Path,
    generation: Generation,
    *,
    machine: MachineIdentity,
) -> None:
    """Serve exactly `generation` through the owned pooler (restart on change)."""
    if not settings.data_plane.pgbouncer_enabled:
        return
    from base.cluster.authority import read_pooler_admin, render_userlist

    rc = pgbouncer.ensure_pgbouncer(
        pg_port=rec.ports["postgres"],
        listen_port=rec.ports["pgbouncer"],
        db_name=database,
        userlist=render_userlist(home, generation),
        admin_password=read_pooler_admin(home).password,
        machine=machine,
    )
    if rc:
        raise RuntimeError("pooler did not become ready")


def prove_generation_logins(home: Path, generation: Generation, endpoint: str) -> None:
    """Both logins of `generation` answer `SELECT 1` through the consumer endpoint
    (the pooler when enabled: SCRAM client auth plus the pass-through hop)."""
    from base.cluster.authority import read_secret
    from base.host.net.url_secret import url_with_userinfo
    from cli.commands.converge.health_preflight import probe_postgres

    secret = read_secret(home, generation)
    for role in (secret.roles.gateway, secret.roles.runner):
        if error := probe_postgres(url_with_userinfo(endpoint, role.name, role.password)):
            raise RuntimeError(f"write generation {generation.number} login {role.name}: {error}")


def adopt_gateway_login(home: Path, endpoint: str) -> None:
    """Point this operator process at the home's active gateway login.

    A start that births (or first admits) the generation has no delivery from
    its own boot, which ran before the ledger existed."""
    import os

    from base.cluster.authority import GENERATION_ENV, write_grant

    grant = write_grant(home, "gateway")
    os.environ["AVA_DB_URL"] = grant.dsn(endpoint)
    os.environ[GENERATION_ENV] = str(grant.number)
    settings.data_plane.db_url = os.environ["AVA_DB_URL"]


def _birth_generation(conn: psycopg.Connection[Any], home: Path, database: str) -> Generation:
    """Birth: groups, monitor, ledger, generation 0."""
    from functools import partial

    from base import cluster
    from base.cluster import authority

    owner = cluster.db_identity()
    groups = authority.Groups(gateway=authority.GATEWAY_GROUP, runner=authority.RUNNER_GROUP)
    authority.ensure_groups(conn, owner=owner, database=database, groups=groups)
    authority.ensure_monitor(conn, database=database)
    authority.create_ledger(home, owner=owner, groups=groups)
    authority.ensure_pooler_admin(home, encrypt=partial(authority.scram_verifier, conn))
    authority.mint_generation(conn, home)
    generation = authority.require_ledger(home).generation
    if generation is None:
        raise RuntimeError("birth minted no write generation")
    return generation


def _admitted_generation(
    conn: psycopg.Connection[Any], home: Path, database: str, *, refresh: bool
) -> Generation:
    """Ordinary start: re-grant after migrations, converge the monitor, then the
    invariant holds."""
    from base import cluster
    from base.cluster import authority

    ledger = authority.load_ledger(home)
    if ledger is None:
        raise RuntimeError(legacy_home_refusal(home))
    if ledger.owner != cluster.db_identity():
        raise RuntimeError(
            f"the database authority ledger records owner {ledger.owner!r}, but AVA_DB_URL "
            f"names database {cluster.db_identity()!r}"
        )
    if ledger.active is None:
        raise RuntimeError(
            "the database authority has no active generation; the interrupted birth must "
            "finish before an ordinary start"
        )
    if refresh:
        authority.ensure_groups(conn, owner=ledger.owner, database=database, groups=ledger.groups)
        authority.ensure_monitor(conn, database=database)
    authority.check_invariant(conn, home, database=database, readonly_grantees=READONLY_GRANTEES)
    return ledger.active


def complete_gateway_data_plane(
    *,
    refresh_schema: bool = True,
    database_factory: DatabaseFactory,
    machine: MachineIdentity,
) -> None:
    """Grant the final schema, establish the write generation, start the pooler,
    then verify consumer credentials.

    Local plane, first start (birth, `needs_provision`):
    groups -> retire legacy logins -> ledger -> mint generation 0 -> pooler
    serving exactly that pair, proven by a pooled login of each -> activate.
    Every step is idempotent, so an interrupted birth retries to the same
    generation. Ordinary start: groups re-granted after migrations, then the
    fail-closed catalog invariant; the pooler serves the active
    pair (restarted only when its bytes change). A home without a ledger is a
    legacy home and refuses (`legacy_home_refusal`) — never converted here.
    """
    from base import cluster
    from cli.commands.converge.health_preflight import probe_postgres, probe_redis
    from cli.start_identity import mark_phase, needs_provision

    if settings.data_plane.is_remote:
        if refresh_schema:
            prepare_memory_vectors(database_factory=database_factory)
        from base.cluster.derive import runner_db_url_projection

        urls = [settings.data_plane.db_url, runner_db_url_projection()]
        for url in urls:
            if error := probe_postgres(url):
                raise RuntimeError(f"consumer database readiness failed: {error}")
    else:
        from base.cluster import authority

        home = ava_home().resolve()
        rec = cluster.get_record(ava_home())
        if rec is None:
            raise RuntimeError("start lost its port reservation")
        database = cluster.db_identity()
        endpoint = db_endpoint()
        if refresh_schema:
            cluster.ensure_pgvector_extension(
                database,
                base_admin_url=instance.pg_admin_url(rec.ports["postgres"]),
                expected_data_dir=ava_home() / "pg",
            )
            prepare_memory_vectors(database_factory=database_factory)
        birth = needs_provision(ava_home())
        if not birth and authority.load_ledger(home) is None:
            raise RuntimeError(legacy_home_refusal(home))
        with admin_session(rec, database) as conn:
            if birth:
                generation = _birth_generation(conn, home, database)
            else:
                generation = _admitted_generation(conn, home, database, refresh=refresh_schema)
            _ensure_pooler(rec, database, home, generation, machine=machine)
            prove_generation_logins(home, generation, endpoint)
            if authority.require_ledger(home).active is None:
                verified = authority.verify_generation(conn, home)
                authority.activate(home, verified)
        adopt_gateway_login(home, endpoint)
        print(f"  ✓ database write generation {generation.number} admitted")
    if error := probe_redis(settings.data_plane.redis_url):
        raise RuntimeError(f"consumer Redis readiness failed: {error}")
    mark_phase(ava_home(), "provisioned")


def print_data_plane_status(*, database_factory: DatabaseFactory) -> None:
    """Print this cluster's own pg/redis reachability for `ava status`.

    Postgres is probed in two stages: `pg_isready` (the server accepts connections)
    then an authenticated `SELECT 1` over the cluster's POOLED front door
    (`connect()` — PgBouncer when enabled, the direct URL when not; the same swap
    every consumer dials) as this process's delivered gateway login. A server that
    is up but refuses that login reports `✗ ... connect failed` rather than a
    false `✓`: `pg_isready` alone answers without authenticating. Redis pings with
    its admin password. Both ports come from this cluster's own db_url / redis_url
    (its per-cluster instance). The pooler line authenticates to its admin console
    with the operator entry from the home's database authority store.

    The probe goes POOLED, never `direct=True` (user ruling 2026-08: every consumer
    sits behind PgBouncer): the pooled `SELECT 1` proves the path consumers
    actually use — client SCRAM against the userlist plus the SCRAM pass-through
    backend hop. With PgBouncer disabled `pooled_db_url == db_url` and the probe
    is direct anyway."""

    if settings.data_plane.is_remote:
        # A remote-managed plane has no local instance to manage — probe the
        # URLs themselves (the switch) and skip the local pooler line.
        print("  · data plane remote-managed — probing the URLs (no local instance)")
        pg_ok, pg_line = remote_pg_reachable(database_factory=database_factory)
        print(f"  {'✓' if pg_ok else '✗'} {pg_line}")
        redis_ok, redis_line = remote_redis_reachable()
        print(f"  {'✓' if redis_ok else '✗'} {redis_line}")
        return

    # Both probes dial the host their own URL names (self-host URLs are
    # loopback-rewritten by DataPlaneSettings). The remote branch above returns
    # before this local probe runs.
    pg_host = instance.pg_dial_host()
    redis_host = instance.redis_dial_host()
    pg_port = urlsplit(settings.data_plane.db_url).port or 5432
    redis_port = urlsplit(settings.data_plane.redis_url).port or 6379
    if not instance.pg_running(pg_port, pg_host):
        print(f"  ✗ postgres ({pg_host}:{pg_port}) unreachable")
    else:
        try:
            with database_factory().connect() as conn:  # pooled front door (PgBouncer when enabled)
                conn.execute("select 1")
            print(f"  ✓ postgres ({pg_host}:{pg_port})")
        except Exception as exc:
            lines = [ln.strip() for ln in str(exc).splitlines() if ln.strip()]
            detail = lines[-1] if lines else "connection failed"
            print(f"  ✗ postgres ({pg_host}:{pg_port}) reachable but connect failed: {detail}")
    print(
        f"  {'✓' if instance.redis_reachable(redis_port, redis_host) else '✗'} "
        f"redis ({redis_host}:{redis_port})"
    )
    if settings.data_plane.pgbouncer_enabled:
        _print_pooler_status()


def _print_pooler_status() -> None:
    """The pooler line: its registry-derived listen port, probed through the
    admin console as the operator entry from the home's authority store. No
    registry record means the port is unknowable — say so instead of a false `:0`."""
    from base.cluster import get_record
    from base.cluster.authority import AuthorityRefusedError, read_pooler_admin

    rec = get_record(ava_home())
    if rec is None:
        print("  - pgbouncer: no registry record — cannot resolve its port")
        return
    port = rec.ports["pgbouncer"]
    try:
        ok = pooler.pgbouncer_listener_reachable(port, read_pooler_admin(ava_home()).password)
    except AuthorityRefusedError as exc:
        print(f"  ✗ pgbouncer (127.0.0.1:{port}): {exc}")
        return
    print(f"  {'✓' if ok else '✗'} pgbouncer (127.0.0.1:{port}, transaction pooling)")


def stop_cluster_instance(
    *,
    retained_children: list[subprocess.Popen[bytes]] | None = None,
    producer: Callable[[], EventPipeline],
) -> int:
    """Stop this cluster's own Postgres + Redis (data preserved on disk). The
    counterpart of ensure_cluster_storage for `ava stop` of a cluster running its
    own instance. Best-effort: a not-running instance is a
    no-op success only after native custody closes."""
    if settings.data_plane.is_remote:
        # A remote-managed plane has no local instance to stop — nothing on this
        # box to tear down, and the provider owns the service lifecycle. But a
        # cluster that SWITCHED local→remote may still have its old local
        # instance running; warn so it is torn down deliberately.
        print(f"\n→ data plane remote-managed ({remote_plane_host()}) — nothing to stop locally")
        warn_orphaned_local_instance()
        return 0
    data = instance.pg_data_dir()
    port = ownership.configured_redis_port()
    # Resolve the admin credential before any stop effect: the runtime URL's
    # ACL user can neither AUTH as `default` nor SHUTDOWN (`-@dangerous`).
    redis_env = (
        None if port is None else instance.redis_cli_env(settings.data_plane.redis_admin_password)
    )
    print("\n→ stopping per-cluster data plane")
    # Stop the pooler first (best-effort, no-op if it was never enabled) so clients
    # are disconnected before Postgres goes down.
    pgbouncer.stop_pgbouncer()
    from cli.commands.lifecycle.service_stop import (
        PROCESS_CLEANUP_WAIT_S,
        PROCESS_KILL_WAIT_S,
        report_postgres_stop_escalation,
    )

    escalation = owned_postgres.stop(
        data,
        immediate_wait=PROCESS_CLEANUP_WAIT_S,
        kill_wait=PROCESS_KILL_WAIT_S,
        retained_children=retained_children,
    )
    if escalation is not None:
        report_postgres_stop_escalation(escalation, producer=producer)
    print("  ✓ postgres stopped")
    if port is not None:
        subprocess.run(
            [instance.redis_cli_bin(), "-p", str(port), "shutdown", "nosave"],
            env=redis_env,
            check=False,
            capture_output=True,
        )
        print("  ✓ redis stopped")
    return 0
