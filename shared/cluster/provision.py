"""Data-plane provisioning — per-cluster Postgres role/db + redis ACL.

The idempotent ensure machinery every bring-up runs: create/re-affirm the
cluster's Postgres role + owned database and apply db/schema.sql as that role
(`ensure_cluster_role` / `provision_database` / `drop_database` — the inverse
and half-built rollback), the redis ACL user scoped to the cluster's keys
and pub/sub channels (`ensure_cluster_redis_acl`), and the least-privilege
`ava_runner` role the runner processes dial after the role-based credential
cutover (`ensure_runner_role`, backed by `ensure_checkpoint_schema` for the
LangGraph checkpoint tables the grants target). Identities are names-as-
data: callers read them from the cluster's own `.env` URLs (`identity_from_url`)
or pass the fixed `DATA_PLANE_IDENTITY` at birth, never from a cluster name.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from shared.url_secret import url_with_userinfo


def _swap_db(url: str, db_name: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, f"/{db_name}", parts.query, parts.fragment))


def _schema_applied(admin_url: str, target: str) -> bool:
    """True if `target` DB has the schema fully applied — `schema_migrations` is
    the last table created by db/schema.sql, so its presence means the apply did
    not fail partway."""
    import psycopg

    with psycopg.connect(_swap_db(admin_url, target)) as conn:
        row = conn.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name = 'schema_migrations'"
        ).fetchone()
    return row is not None


def ensure_cluster_role(identity: str, *, base_admin_url: str, cluster_secret: str) -> None:
    """Create (or re-affirm) the cluster's data-plane role `identity` and make it
    own the database of the same name. Idempotent — safe on every bring-up.

    `identity` is names-as-data: the caller reads it from the cluster's own
    db_url (`identity_from_url`) for an existing cluster, or passes
    `DATA_PLANE_IDENTITY` at birth — it is never derived from a cluster name, so
    prod's historical `ava_main` keeps re-affirming until an ops rename.

    The role is `LOGIN NOSUPERUSER` (so it bypasses no grant and can reach only
    its own database) and its password is (re)set to the current cluster secret
    every call, so a rotation self-heals. One exception: when the OS user the
    install ran as IS the cluster identity (e.g. a host user named `ava`), the
    role is the initdb bootstrap superuser, which Postgres refuses to downgrade
    — it stays SUPERUSER on its own single-tenant instance. The instance is the cluster's own, so
    this can never touch another cluster's role. When the database already
    exists (an existing cluster, or the legacy single-`ava`-role layout),
    ownership is adopted so the role can run migrations against it.

    base_admin_url must connect as a Postgres superuser — the loopback-`trust`
    bootstrap superuser — to a maintenance db (e.g. `postgres`) on the same instance.
    """
    import psycopg
    from psycopg import sql as pgsql

    with psycopg.connect(base_admin_url, autocommit=True) as conn:
        has_role = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (identity,)).fetchone()
        # The initdb bootstrap superuser is the installing OS user. When that user
        # is ALSO the cluster identity (a host user literally named like the fixed
        # `ava` identity), the role already exists as the bootstrap superuser, and
        # Postgres refuses to downgrade the bootstrap superuser (oid 10):
        # "permission denied to alter role ... the bootstrap superuser must have
        # the SUPERUSER attribute". In that one case skip the downgrade — the role
        # stays SUPERUSER on its own single-tenant instance. Every other install
        # (identity != OS user) keeps the NOSUPERUSER posture unchanged.
        is_bootstrap = conn.execute(
            "SELECT 1 FROM pg_roles WHERE rolname = %s AND oid = 10", (identity,)
        ).fetchone()
        # pgsql.Literal quotes the secret as a string literal (the only safe way to
        # put a password in ALTER/CREATE ROLE — it is not a bind-param position).
        conn.execute(
            pgsql.SQL("{} ROLE {} LOGIN {} PASSWORD {}").format(
                pgsql.SQL("ALTER" if has_role else "CREATE"),
                pgsql.Identifier(identity),
                pgsql.SQL("" if is_bootstrap else "NOSUPERUSER"),
                pgsql.Literal(cluster_secret),
            )
        )
        db_exists = conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (identity,)
        ).fetchone()
    if db_exists:
        _adopt_database(base_admin_url, identity, identity)


def _adopt_database(base_admin_url: str, target: str, owner: str) -> None:
    """Make `owner` own database `target` and every object in it — the migration
    from the legacy single shared `ava` role to a per-cluster owner. Idempotent:
    re-running once the legacy `ava` role owns nothing in `target` is a no-op (and
    a no-op entirely when the legacy role never existed, e.g. a fresh install)."""
    import psycopg
    from psycopg import sql as pgsql

    with psycopg.connect(base_admin_url, autocommit=True) as conn:
        conn.execute(
            pgsql.SQL("ALTER DATABASE {} OWNER TO {}").format(
                pgsql.Identifier(target), pgsql.Identifier(owner)
            )
        )
    with psycopg.connect(_swap_db(base_admin_url, target), autocommit=True) as conn:
        # Reassign the legacy `ava` role's objects in `target` to the new owner.
        # Only when `ava` exists AND is NOT the bootstrap superuser (oid 10): the
        # bootstrap owns pinned system catalogs that REASSIGN OWNED cannot touch
        # ("required by the database system"), and it is never the legacy app role
        # we are migrating from (that `ava` was a separately-created superuser).
        # REASSIGN OWNED operates on the current database only, so it never reaches
        # another cluster's db. Skipped when owner == 'ava' (a fresh path-only
        # cluster: the role already owns its objects; reassigning to itself is
        # meaningless).
        legacy = conn.execute(
            "SELECT 1 FROM pg_roles WHERE rolname = 'ava' AND oid <> 10"
        ).fetchone()
        if legacy and owner != "ava":
            conn.execute(pgsql.SQL("REASSIGN OWNED BY ava TO {}").format(pgsql.Identifier(owner)))


def provision_database(identity: str, *, base_admin_url: str, cluster_secret: str) -> None:
    """Atomically provision a cluster's Postgres database AND its owning role:
    ensure role `identity`, CREATE DATABASE `identity` OWNED BY it, and apply
    db/schema.sql *as that role* so every object is role-owned. `identity` is the
    shared db/role identifier (names-as-data — `DATA_PLANE_IDENTITY` at birth).
    base_admin_url must connect as the loopback-`trust` bootstrap superuser to a
    maintenance db (`postgres`) on the same Postgres.

    Idempotent for a fully-provisioned DB (the role is re-affirmed + ownership
    adopted, then it returns). If the DB exists but its schema is incomplete (a
    prior apply crashed mid-way), this raises rather than silently treating it as
    ready. Provisioning is atomic: if schema apply fails on a freshly-created DB,
    the DB is dropped so a retry starts clean.

    Raises:
        RuntimeError: the DB exists but schema_migrations is missing (half-provisioned).
    """
    import psycopg
    from psycopg import sql as pgsql

    ensure_cluster_role(identity, base_admin_url=base_admin_url, cluster_secret=cluster_secret)
    with psycopg.connect(base_admin_url, autocommit=True) as conn:
        exists = conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (identity,)
        ).fetchone()
    if exists:
        if _schema_applied(base_admin_url, identity):
            return  # ensure_cluster_role already (re)affirmed the role + adopted ownership
        raise RuntimeError(
            f"database {identity!r} exists but its schema is incomplete (a prior "
            f"provision failed mid-apply). Drop it and retry: "
            f'DROP DATABASE "{identity}".'
        )

    with psycopg.connect(base_admin_url, autocommit=True) as conn:
        # db and role share the identifier; sql.Identifier quotes it.
        conn.execute(
            pgsql.SQL("CREATE DATABASE {} OWNER {}").format(
                pgsql.Identifier(identity), pgsql.Identifier(identity)
            )
        )
    schema_sql = (Path(__file__).resolve().parents[2] / "db" / "schema.sql").read_text()
    try:
        # Apply the schema AS the cluster role so every object is owned by the role,
        # not the bootstrap superuser — the role must own them to run later
        # migrations. Connect with the role + its secret: over loopback `trust` the
        # password is ignored, over scram it is checked (the role was just created
        # with it), so this works both in prod and against a password-auth pg.
        # schema.sql is a trusted multi-statement script read from disk; same
        # pattern as shared/migrations.py applying a body.
        role_url = url_with_userinfo(_swap_db(base_admin_url, identity), identity, cluster_secret)
        with psycopg.connect(role_url, autocommit=True) as conn:
            conn.execute(schema_sql)  # type: ignore[arg-type]
    except Exception:
        # Drop the half-built DB so the next provision attempt starts clean
        # rather than tripping the "exists but incomplete" guard above.
        drop_database(identity, base_admin_url=base_admin_url)
        raise


def drop_database(identity: str, *, base_admin_url: str) -> None:
    """DROP DATABASE `identity` + its owning role of the same name — the inverse
    of provision_database, and its half-built rollback. base_admin_url must
    connect as the loopback-`trust` bootstrap superuser to a maintenance db
    (`postgres`) on the same Postgres.

    Idempotent (IF EXISTS). The caller is responsible for there being no live
    connections to the target (in prod `ava cluster destroy` runs after the
    cluster is stopped); this does not force-terminate backends. The role is
    dropped after the database so it owns nothing and the drop cannot fail on a
    dependency.
    """
    import psycopg
    from psycopg import sql as pgsql

    with psycopg.connect(base_admin_url, autocommit=True) as conn:
        conn.execute(pgsql.SQL("DROP DATABASE IF EXISTS {}").format(pgsql.Identifier(identity)))
        conn.execute(pgsql.SQL("DROP ROLE IF EXISTS {}").format(pgsql.Identifier(identity)))


def ensure_checkpoint_schema(identity: str, *, base_admin_url: str, cluster_secret: str) -> None:
    """Create the LangGraph checkpoint tables (idempotent) AS the cluster role.

    Runs the same `PostgresSaver.setup()` an agent boot runs, so the tables are
    owned by the cluster's MAIN role — the gateway's checkpoint readers dial
    that role. Called at install birth BEFORE `ensure_runner_role`: the runner's
    table grants can only target existing tables, and a runner booted as
    `ava_runner` (post-cutover) finds the tables already present — its boot then
    skips setup() (`agent/_process_boot._checkpoint_schema_present`), because
    Postgres refuses `CREATE TABLE IF NOT EXISTS` for a role without CREATE on
    the schema even when the tables exist (the runner holds no CREATE, by
    design — any DDL must fail under it).

    Idempotent: on an existing cluster whose checkpoint tables exist (created by
    any earlier agent boot), setup() is a no-op.
    """
    from langgraph.checkpoint.postgres import PostgresSaver

    from shared.url_secret import url_with_userinfo

    # Same role-URL pattern as provision_database: connect AS the cluster role
    # (its own schema objects), over loopback trust or scram with the secret.
    # from_conn_string owns the connection for the setup (the same construction
    # shared/pg_tools.py uses for throwaway test clusters).
    role_url = url_with_userinfo(_swap_db(base_admin_url, identity), identity, cluster_secret)
    with PostgresSaver.from_conn_string(role_url) as saver:
        saver.setup()


def ensure_runner_role(identity: str, *, base_admin_url: str, runner_password: str) -> None:
    """Create (or re-affirm) the cluster's `ava_runner` least-privilege role and
    its table grants. Idempotent — install birth and `ava cluster
    ensure-db-role` run the same SQL (Task #1236 design).

    The role is `LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE` with
    `runner_password` (the gateway `.env` AVA_RUNNER_DB_PASSWORD value,
    re-affirmed every call so a `.env` edit self-heals). The grants give the
    runner processes exactly their audited surface:

      - SELECT on every table in public (the runner read surface: agents,
        tasks, notices, ...), and — as a standing `ALTER DEFAULT PRIVILEGES`
        beside it — on every table a later migration adds. The `ALL TABLES`
        form alone covers only what exists at grant time; see the comment at
        the two ALTERs for why that gap is invisible until it bites.
      - SELECT, UPDATE, INSERT on inbound_messages — claim polling AND the
        agent-side self-lifecycle inbounds (`ava.self.terminate` / `restart` /
        `compact` insert their own 'terminate' / 'restart' / 'compact_summary'
        rows; e2e caught the missing INSERT: an agent whose self-terminate
        inbound could not land stayed 'running' forever)
      - SELECT, UPDATE on agents_meta (status/pid/liveness; INSERT stays with
        gateway spawn) and agents (UPDATE is `ava.self.set_label` writing the
        agent's OWN row; INSERT stays with gateway spawn)
      - INSERT, UPDATE, SELECT on machine_units AND INSERT, UPDATE on machines
        (register_self / mark_stopping — `ava start` / `ava stop` on every
        unit, runner included)
      - INSERT, UPDATE on host_deploy_state (set_posture — every `ava start`)
      - INSERT, UPDATE, DELETE on api_idempotency (the runner's ops server
        dedupes inbound /ops calls)
      - INSERT, UPDATE on agent_tasks (`ava.tasks`) and agent_watchers
        (`ava.watcher`), UPDATE on agent_pages (page close at exit) — the SDK
        surfaces the agent process writes directly
      - ALL on the LangGraph checkpoint tables (agent state: checkpoints,
        checkpoint_blobs, checkpoint_writes)

    Everything else — agents INSERT, agents_meta INSERT, any DDL, writes to
    notices / ops_alerts / cluster_* / config tables — fails with a permission
    error under this role (the runner's self-update bookkeeping is file-based,
    so deployment_state / cluster_last_update stay gateway-only): the 2026-08-12 pollution class (agents + agents_meta
    INSERT with the full prod write credential) is structurally impossible
    once runners dial this role. (notices/agent_pages INSERTs travel over the
    gateway HTTP API as the main identity, never from the runner role.)

    `identity` is the cluster's main db/role identifier (names-as-data), and
    also the role the default privileges are declared FOR — it is what
    migrations run as, and default privileges key on the creating role. The
    checkpoint tables must already exist (see `ensure_checkpoint_schema`); a
    missing table makes the grant fail loudly rather than silently narrowing
    the contract.

    Re-running this is how an EXISTING cluster picks up tables added since its
    birth: `ava start` calls it on a gateway host after applying a migration,
    and `ava cluster ensure-db-role` is the manual door. The standing
    default privileges only take effect for tables created after they are
    declared, so the re-run is what closes the retroactive half.
    """
    import psycopg
    from psycopg import sql as pgsql

    from shared.cluster.derive import RUNNER_ROLE

    with psycopg.connect(base_admin_url, autocommit=True) as conn:
        has_role = conn.execute(
            "SELECT 1 FROM pg_roles WHERE rolname = %s", (RUNNER_ROLE,)
        ).fetchone()
        # pgsql.Literal quotes the password as a string literal (the only safe
        # way to put a password in ALTER/CREATE ROLE — not a bind-param position).
        conn.execute(
            pgsql.SQL("{} ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD {}").format(
                pgsql.SQL("ALTER" if has_role else "CREATE"),
                pgsql.Identifier(RUNNER_ROLE),
                pgsql.Literal(runner_password),
            )
        )
    with psycopg.connect(_swap_db(base_admin_url, identity), autocommit=True) as conn:
        conn.execute(
            pgsql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA public TO {}").format(
                pgsql.Identifier(RUNNER_ROLE)
            )
        )
        # Sequence USAGE so the runner's INSERTs can draw identity ids
        # (inbound_messages / agent_tasks are BIGSERIAL; table-level INSERT
        # grants do not cover the owning sequence).
        conn.execute(
            pgsql.SQL("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {}").format(
                pgsql.Identifier(RUNNER_ROLE)
            )
        )
        # The two ALL grants above are a point-in-time LOOP over what exists
        # right now — Postgres expands them into per-object ACL entries and
        # nothing carries forward. A table a later migration creates is
        # therefore invisible to the runner until somebody re-runs this
        # function, which nothing does on a schedule. That gap was live and
        # unnoticed until the first post-baseline migration to CREATE a table
        # (`20260820T175737_extension-registry.sql`); every earlier one only
        # added columns to tables the birth grant already covered.
        #
        # These two ALTER DEFAULT PRIVILEGES are the standing form of the same
        # policy, so the read surface stays whole by construction. `FOR ROLE
        # {identity}` is load-bearing: default privileges key on the role that
        # CREATES the object, not on the connection issuing the ALTER, and
        # migrations run as the cluster's main identity while this call dials
        # as the instance admin. Without it the policy would attach to the
        # admin and never fire.
        #
        # They do NOT retroactively grant anything, so an existing cluster
        # still needs the re-run above to cover tables it already has — that
        # is what `ava start` triggers after applying a migration, and what
        # `ava cluster ensure-db-role` does by hand.
        conn.execute(
            pgsql.SQL(
                "ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA public GRANT SELECT ON TABLES TO {}"
            ).format(pgsql.Identifier(identity), pgsql.Identifier(RUNNER_ROLE))
        )
        conn.execute(
            pgsql.SQL(
                "ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA public"
                " GRANT USAGE, SELECT ON SEQUENCES TO {}"
            ).format(pgsql.Identifier(identity), pgsql.Identifier(RUNNER_ROLE))
        )
        for table in ("inbound_messages", "agents_meta"):
            conn.execute(
                pgsql.SQL("GRANT SELECT, UPDATE ON {} TO {}").format(
                    pgsql.Identifier(table), pgsql.Identifier(RUNNER_ROLE)
                )
            )
        # The agent-side self-lifecycle inbounds (terminate / restart / compact)
        # INSERT directly from the runner process — not via the gateway API.
        conn.execute(
            pgsql.SQL("GRANT INSERT ON inbound_messages TO {}").format(
                pgsql.Identifier(RUNNER_ROLE)
            )
        )
        # ava.self.set_label UPDATEs the agent's own agents row.
        conn.execute(
            pgsql.SQL("GRANT UPDATE ON agents TO {}").format(pgsql.Identifier(RUNNER_ROLE))
        )
        conn.execute(
            pgsql.SQL("GRANT INSERT, UPDATE, SELECT ON machine_units TO {}").format(
                pgsql.Identifier(RUNNER_ROLE)
            )
        )
        # The runner service chain's writes (prod-deploy finding, #2599 follow-up):
        # register_self / mark_stopping (ava start / stop) touch `machines`, every
        # start writes the deploy posture, and the runner's ops server dedupes
        # /ops calls through api_idempotency.
        conn.execute(
            pgsql.SQL("GRANT INSERT, UPDATE ON machines TO {}").format(
                pgsql.Identifier(RUNNER_ROLE)
            )
        )
        conn.execute(
            pgsql.SQL("GRANT INSERT, UPDATE ON host_deploy_state TO {}").format(
                pgsql.Identifier(RUNNER_ROLE)
            )
        )
        conn.execute(
            pgsql.SQL("GRANT INSERT, UPDATE, DELETE ON api_idempotency TO {}").format(
                pgsql.Identifier(RUNNER_ROLE)
            )
        )
        # SDK surfaces the runner process writes directly: ava.tasks,
        # ava.watcher, and the page close at exit.
        for table in ("agent_tasks", "agent_watchers"):
            conn.execute(
                pgsql.SQL("GRANT INSERT, UPDATE ON {} TO {}").format(
                    pgsql.Identifier(table), pgsql.Identifier(RUNNER_ROLE)
                )
            )
        conn.execute(
            pgsql.SQL("GRANT UPDATE ON agent_pages TO {}").format(pgsql.Identifier(RUNNER_ROLE))
        )
        for table in ("checkpoints", "checkpoint_blobs", "checkpoint_writes"):
            conn.execute(
                pgsql.SQL("GRANT ALL ON {} TO {}").format(
                    pgsql.Identifier(table), pgsql.Identifier(RUNNER_ROLE)
                )
            )


def ensure_cluster_redis_acl(
    user: str, *, redis_admin_url: str, cluster_secret: str, channel_prefix: str
) -> None:
    """Create (or re-affirm) the cluster's redis ACL user `user` — the runtime
    redis identity, mirroring the per-cluster Postgres role. Idempotent; safe on
    every bring-up. `user` is names-as-data: read from the cluster's own
    redis_url (`identity_from_url`) for an existing cluster, `DATA_PLANE_IDENTITY`
    at birth. The user authenticates with the cluster secret and is scoped
    to keys (`~*`) + pub/sub channels (`&<channel_prefix>:*`); `-@dangerous` denies
    FLUSHALL / CONFIG / SHUTDOWN. The secret travels over the redis connection, never
    a process argv.

    `resetpass` precedes `>cluster_secret`: Redis ACL passwords are additive by
    default (`>password` ADDS a valid password rather than replacing the set), so
    without it a secret rotation would leave the PREVIOUS secret still
    authenticating this user indefinitely — confirmed empirically while building
    `scripts/rotate_cluster_secret.py`. `resetpass` clears the password list first,
    so re-affirming with an unchanged secret still ends at exactly one valid
    password (this call is idempotent either way), and re-affirming with a
    rotated one actually invalidates the old one.

    Empty secret (single-box no-auth): the user is created with `nopass` instead
    of a password. The runtime URLs still carry the identity as username
    (names-as-data holds with or without auth), and a URL with a username makes
    redis-py send AUTH — a missing user would WRONGPASS forever and the wake bus
    would never deliver. `nopass` lets that AUTH succeed while the posture stays
    unauthenticated (requirepass is off and the `default` user is nopass too).

    redis_admin_url connects as the redis `default` (admin) user, whose password is
    the cluster secret (each cluster's redis is single-tenant, so `requirepass` == the
    cluster secret)."""
    import redis

    # redis-py types from_url's **kwargs as Unknown; the call itself is fully typed.
    client = redis.Redis.from_url(redis_admin_url, decode_responses=True)  # pyright: ignore[reportUnknownMemberType]
    try:
        # redis-py types execute_command()'s signature as partially Unknown; the call is fully typed.
        client.execute_command(  # pyright: ignore[reportUnknownMemberType]
            "ACL",
            "SETUSER",
            user,
            "on",
            "resetpass",
            f">{cluster_secret}" if cluster_secret else "nopass",
            "resetkeys",
            "~*",
            "resetchannels",
            f"&{channel_prefix}:*",
            "+@all",
            "-@dangerous",
        )
    finally:
        client.close()
