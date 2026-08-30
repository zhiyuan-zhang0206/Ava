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

from shared.log import logger
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


def ensure_cluster_role(identity: str, *, base_admin_url: str, db_admin_password: str) -> None:
    """Create (or re-affirm) the cluster's data-plane role `identity` and make it
    own the database of the same name. Idempotent — safe on every bring-up.

    `identity` is names-as-data: the caller reads it from the cluster's own
    db_url (`identity_from_url`) for an existing cluster, or passes
    `DATA_PLANE_IDENTITY` at birth — it is never derived from a cluster name, so
    prod's historical `ava_main` keeps re-affirming until an ops rename.

    The role is `LOGIN NOSUPERUSER` (so it bypasses no grant and can reach only
    its own database) and its password is (re)set to the current DB owner password
    every call, so an owner-password rotation self-heals. One exception: when the OS user the
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
        # pgsql.Literal quotes the password as a string literal (the only safe way to
        # put a password in ALTER/CREATE ROLE — it is not a bind-param position).
        conn.execute(
            pgsql.SQL("{} ROLE {} LOGIN {} PASSWORD {}").format(
                pgsql.SQL("ALTER" if has_role else "CREATE"),
                pgsql.Identifier(identity),
                pgsql.SQL("" if is_bootstrap else "NOSUPERUSER"),
                pgsql.Literal(db_admin_password),
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


def provision_database(identity: str, *, base_admin_url: str, db_admin_password: str) -> bool:
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

    Returns:
        True only when this call created the database; False when it adopted an
        already-provisioned database. Birth uses this exact database provenance
        to decide whether a later checkpoint-setup failure may drop the database.

    Raises:
        RuntimeError: the DB exists but schema_migrations is missing (half-provisioned).
    """
    import psycopg
    from psycopg import sql as pgsql

    ensure_cluster_role(
        identity, base_admin_url=base_admin_url, db_admin_password=db_admin_password
    )
    with psycopg.connect(base_admin_url, autocommit=True) as conn:
        exists = conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (identity,)
        ).fetchone()
    if exists:
        if _schema_applied(base_admin_url, identity):
            return False  # role already re-affirmed and ownership adopted
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
        role_url = url_with_userinfo(
            _swap_db(base_admin_url, identity), identity, db_admin_password
        )
        with psycopg.connect(role_url, autocommit=True) as conn:
            conn.execute(schema_sql)  # type: ignore[arg-type]
    except Exception:
        # Drop the half-built DB so the next provision attempt starts clean
        # rather than tripping the "exists but incomplete" guard above.
        drop_database(identity, base_admin_url=base_admin_url)
        raise
    return True


def ensure_pgvector_extension(identity: str, *, base_admin_url: str) -> None:
    """Pre-create the pgvector extension in the cluster database with the
    bootstrap-superuser connection (`base_admin_url`), so the NOSUPERUSER
    runtime roles never need to: pgvector's `vector.control` ships without
    `trusted = true`, which makes `CREATE EXTENSION` superuser-only by
    Postgres' own policy (deliberately not overridden on the injected control
    file). Idempotent — `ava start` runs it on every bring-up and install
    birth runs it once, so an existing cluster picks the extension up on its
    next start, and the indexer's NOSUPERUSER `CREATE EXTENSION IF NOT
    EXISTS` stays a harmless no-op (verified against a real injected tree).

    A Postgres that does not carry the extension binaries (a remote-managed
    plane, a brew/apt install without the pgvector package, or a vendored
    tree from before the injection landed) is a silent no-op — the memory
    indexer's startup preflight owns that failure surface with its actionable
    message. Deliberately not a migration: migrations must apply against
    Postgres installations that have no pgvector at all.

    An unreachable admin connection is a no-op too (logged, not raised): the
    ensure re-runs on every bring-up, so a transient dead socket retries next
    start, and the migrations step right behind it is the loud failure path
    when the data plane is genuinely gone.
    """
    import psycopg

    try:
        with psycopg.connect(base_admin_url, autocommit=True) as conn:
            available = conn.execute(
                "SELECT 1 FROM pg_available_extensions WHERE name = 'vector'"
            ).fetchone()
        if available is None:
            return
        with psycopg.connect(_swap_db(base_admin_url, identity), autocommit=True) as conn:
            conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
    except psycopg.OperationalError as exc:
        logger.warning(
            "[pgvector] pre-create skipped: cluster Postgres unreachable over the "
            "admin connection (%s) — re-attempted on the next bring-up",
            exc,
        )


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


# Frozen at the upstream schema present when Ava adopted reversible checkpoint
# migrations. Never bump this baseline: future versions belong in the manifest
# below and must name their paired Ava up/down migration.
CHECKPOINT_SCHEMA_UPSTREAM_BASELINE_VERSION = 9
CHECKPOINT_SCHEMA_AVA_MIGRATIONS: dict[int, str] = {}


class CheckpointSchemaError(RuntimeError):
    """The LangGraph checkpoint schema cannot safely serve this checkout."""


class CheckpointDependencyDriftError(CheckpointSchemaError):
    """The dependency added schema migrations without a paired Ava migration."""


class CheckpointSchemaMismatchError(CheckpointSchemaError):
    """The database checkpoint migration set is not exactly current."""


def _expected_checkpoint_schema_versions() -> frozenset[int]:
    """The upstream versions explicitly approved by Ava's rollback contract."""
    from langgraph.checkpoint.postgres import PostgresSaver

    from shared import migrations as ava_migrations

    declared_versions = sorted(CHECKPOINT_SCHEMA_AVA_MIGRATIONS)
    expected_declared = list(
        range(
            CHECKPOINT_SCHEMA_UPSTREAM_BASELINE_VERSION + 1,
            CHECKPOINT_SCHEMA_UPSTREAM_BASELINE_VERSION + 1 + len(declared_versions),
        )
    )
    if declared_versions != expected_declared:
        raise CheckpointDependencyDriftError(
            "checkpoint migration manifest must be contiguous after the frozen "
            f"upstream baseline {CHECKPOINT_SCHEMA_UPSTREAM_BASELINE_VERSION}: "
            f"declared={declared_versions}, expected={expected_declared}"
        )

    declared_names = [CHECKPOINT_SCHEMA_AVA_MIGRATIONS[version] for version in declared_versions]
    if declared_names != sorted(declared_names):
        raise CheckpointDependencyDriftError(
            "checkpoint migration manifest names must follow upstream version order: "
            f"declared={declared_names}, sorted={sorted(declared_names)}"
        )

    tracked = ava_migrations.required_migration_set()
    for version, name in CHECKPOINT_SCHEMA_AVA_MIGRATIONS.items():
        up = ava_migrations.MIGRATIONS_DIR / f"{name}.sql"
        down = ava_migrations.MIGRATIONS_DIR / f"{name}.down.sql"
        if name not in tracked or not up.is_file() or not down.is_file():
            raise CheckpointDependencyDriftError(
                f"checkpoint version {version} must name a git-tracked paired Ava "
                f"migration: name={name!r}, up_exists={up.is_file()}, "
                f"down_exists={down.is_file()}, tracked={name in tracked}"
            )

    approved_target = CHECKPOINT_SCHEMA_UPSTREAM_BASELINE_VERSION + len(declared_versions)
    dependency_target = len(PostgresSaver.MIGRATIONS) - 1
    if dependency_target != approved_target:
        raise CheckpointDependencyDriftError(
            "LangGraph checkpoint migrations changed without an Ava rollback migration: "
            f"dependency target={dependency_target}, "
            f"approved target={approved_target}. Mirror every new "
            "upstream migration in a paired Ava timestamp migration (including the "
            "checkpoint_migrations row), then add it to "
            "CHECKPOINT_SCHEMA_AVA_MIGRATIONS."
        )
    return frozenset(range(approved_target + 1))


def assert_checkpoint_dependency_pinned() -> None:
    """Fail before any DB work when the dependency schema contract drifted."""
    _expected_checkpoint_schema_versions()


def _checkpoint_schema_versions(db_url: str) -> frozenset[int] | None:
    """Return the complete applied set, or ``None`` when no schema exists."""
    import psycopg

    with psycopg.connect(db_url, autocommit=True) as conn:
        table = conn.execute("SELECT to_regclass('public.checkpoint_migrations')").fetchone()
        if table is None or table[0] is None:
            return None
        rows = conn.execute("SELECT v FROM checkpoint_migrations").fetchall()
    return frozenset(int(row[0]) for row in rows)


def assert_checkpoint_schema_current(db_url: str) -> None:
    """Require the exact approved checkpoint migration set, without mutation.

    Every start role calls this after Ava migrations.  A pure runner therefore
    detects behind, ahead, empty, or internally-gapped checkpoint state without
    acquiring DDL capability.  Ahead is also refused: upstream gives no old-
    runtime/new-schema compatibility guarantee, while Ava rollback can safely
    reverse only schema changes represented by paired Ava migrations.
    """
    expected = _expected_checkpoint_schema_versions()
    actual = _checkpoint_schema_versions(db_url)
    if actual != expected:
        found: frozenset[int] = frozenset() if actual is None else actual
        raise CheckpointSchemaMismatchError(
            "checkpoint schema is not current: "
            f"missing={sorted(expected - found)}, unexpected={sorted(found - expected)}, "
            f"table_present={actual is not None}. Run `ava start` on the gateway; "
            "do not grant schema CREATE to runtime roles."
        )


def ensure_checkpoint_schema(
    identity: str,
    *,
    base_admin_url: str,
    db_admin_password: str,
    database_created: bool = False,
    resume_partial: bool = False,
) -> None:
    """Create the LangGraph checkpoint tables (idempotent) AS the cluster role.

    Runs `PostgresSaver.setup()` as the cluster's MAIN role, so that role owns
    the tables while runtime readers may use either it or `ava_runner`. Called
    at install birth BEFORE `ensure_runner_role`: the runner's
    table grants can only target existing tables, and a runner booted as
    `ava_runner` (post-cutover) never runs setup(): Postgres refuses `CREATE
    TABLE IF NOT EXISTS` for a role without CREATE on the schema even when the
    tables exist (the runner holds no CREATE, by design — any DDL must fail
    under it).

    Setup is install-only. ``database_created`` is the exact result of this
    birth's ``provision_database`` call, not registry state. Upstream setup is
    autocommit, so a failure can leave a contiguous prefix; when this call owns
    the newly-created DB it drops that DB and role, making retry start clean.
    ``resume_partial`` is separate, explicit install-birth authority: a hard
    process death cannot run cleanup, so an idempotent birth retry may continue
    only an exact contiguous prefix. Existing cluster/operator paths leave it
    off and never repair, resume, or drop a partial/older/newer schema. Gaps and
    unknown versions are never resumable.
    """
    from langgraph.checkpoint.postgres import PostgresSaver

    from shared.url_secret import url_with_userinfo

    # Same role-URL pattern as provision_database: connect AS the cluster role
    # (its own schema objects), over loopback trust or scram with the secret.
    # from_conn_string owns the connection for the setup (the same construction
    # shared/pg_tools.py uses for throwaway test clusters).
    role_url = url_with_userinfo(_swap_db(base_admin_url, identity), identity, db_admin_password)
    expected = _expected_checkpoint_schema_versions()
    actual = _checkpoint_schema_versions(role_url)
    if actual == expected:
        return
    may_setup = database_created or resume_partial
    resumable = actual is None or actual == frozenset(range(len(actual)))
    if not may_setup or not resumable:
        assert_checkpoint_schema_current(role_url)
        return
    try:
        with PostgresSaver.from_conn_string(role_url) as saver:
            saver.setup()
        assert_checkpoint_schema_current(role_url)
    except Exception:
        if database_created:
            drop_database(identity, base_admin_url=base_admin_url)
        raise


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
      - SELECT, INSERT on heartbeat_pause_log (ava.self.pause_heartbeat
        logs its window from the runner process: SELECT the previous one,
        INSERT the new row; append-only — no UPDATE/DELETE path, so they
        stay out — task #1932: the table shipped without this entry and
        the fleet-wide pause_heartbeat INSERT failed with
        InsufficientPrivilege)
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
        # A watcher that exits cleanly deletes its OWN registry row from the
        # watcher child's finally (shared/watcher_registry.delete_watcher) —
        # without DELETE the row survives and the boot reconcile later treats
        # the gone session as a killed watcher to rebuild / mark missed
        # (prod finding 2026-08-28: "permission denied for table
        # agent_watchers"). agent_tasks stays INSERT+UPDATE-only: no SDK path
        # deletes task rows from the runner process.
        conn.execute(
            pgsql.SQL("GRANT DELETE ON agent_watchers TO {}").format(pgsql.Identifier(RUNNER_ROLE))
        )
        conn.execute(
            pgsql.SQL("GRANT UPDATE ON agent_pages TO {}").format(pgsql.Identifier(RUNNER_ROLE))
        )
        # Every ava.shell.sessions.new(ttl=) / run_background(ttl=) records its
        # mandatory deadline directly from the runner process; the gateway TTL
        # reaper (main identity) reads and deletes the rows.
        conn.execute(
            pgsql.SQL("GRANT INSERT ON agent_shell_ttls TO {}").format(
                pgsql.Identifier(RUNNER_ROLE)
            )
        )
        # ava.self.pause_heartbeat: the pause trail (SELECT the previous window
        # + INSERT the new row; the sequence USAGE comes from the ALL SEQUENCES
        # grant above). Append-only — no runner path UPDATEs or DELETEs rows,
        # so those stay out. Regression for task #1932: this entry was missing
        # when the table shipped, and every runner's pause_heartbeat INSERT
        # failed with InsufficientPrivilege until prod was patched by hand.
        conn.execute(
            pgsql.SQL("GRANT SELECT, INSERT ON heartbeat_pause_log TO {}").format(
                pgsql.Identifier(RUNNER_ROLE)
            )
        )
        for table in ("checkpoints", "checkpoint_blobs", "checkpoint_writes"):
            conn.execute(
                pgsql.SQL("GRANT ALL ON {} TO {}").format(
                    pgsql.Identifier(table), pgsql.Identifier(RUNNER_ROLE)
                )
            )


def ensure_cluster_redis_acl(
    user: str, *, redis_admin_url: str, runtime_password: str, channel_prefix: str
) -> None:
    """Create (or re-affirm) the cluster's redis ACL user `user` — the runtime
    redis identity, mirroring the per-cluster Postgres role. Idempotent; safe on
    every bring-up. `user` is names-as-data: read from the cluster's own
    redis_url (`identity_from_url`) for an existing cluster, `DATA_PLANE_IDENTITY`
    at birth. The user authenticates with its independent runtime password and is scoped
    to keys (`~*`) + pub/sub channels (`&<channel_prefix>:*` plus the hosted
    dispatcher's `&<channel_prefix>:inbound:*` subscription pattern); `-@dangerous` denies
    FLUSHALL / CONFIG / SHUTDOWN. The secret travels over the redis connection, never
    a process argv.

    `resetpass` precedes `>runtime_password`: Redis ACL passwords are additive by
    default (`>password` ADDS a valid password rather than replacing the set), so
    without it a runtime-password rotation would leave the previous password still
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

    redis_admin_url connects as the Redis `default` user with the independent
    gateway-only Redis admin password."""
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
            f">{runtime_password}" if runtime_password else "nopass",
            "resetkeys",
            "~*",
            "resetchannels",
            f"&{channel_prefix}:*",
            # The hosted dispatcher PSUBSCRIBEs `<prefix>:inbound:*`. Redis
            # checks the subscription PATTERN, not the channels it would match,
            # and `&<prefix>:*` does not cover it (empirically, Redis 8) — the
            # agent-host reconnect-looped on NoPermissionError without this
            # grant (2026-08-30 soak startup).
            f"&{channel_prefix}:inbound:*",
            "+@all",
            "-@dangerous",
        )
    finally:
        client.close()
