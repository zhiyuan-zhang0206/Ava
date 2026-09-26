"""Data-plane provisioning — per-cluster Postgres owner/db + checkpoint schema.

The idempotent ensure machinery every bring-up runs: create/re-affirm the
cluster's NOLOGIN schema owner + owned database and apply db/schema.sql acting
as that owner (`ensure_cluster_role` / `provision_database` / `drop_database`
— the inverse and half-built rollback), and the LangGraph checkpoint tables
(`ensure_checkpoint_schema`). Identities are names-as-data: callers read them
from the cluster's own `.env` URLs (`identity_from_url`) or pass the fixed
`DATA_PLANE_IDENTITY` at birth, never from a cluster name. Application
privileges belong to the capability groups and write generations in
`shared.cluster.authority`, never to the owner.

Every dial is the administrator (`shared.pg_admin`): as itself for roles,
databases and extensions, and acting as the owner (`owner_session`) for the
objects the owner must own. The owner never logs in.
`shared.pg_admin` loads psycopg, so each function imports it: `shared.cluster`
is on the `import ava` path, which stays driver-free (task #3816).
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from shared.log import logger


def _swap_db(url: str, db_name: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, f"/{db_name}", parts.query, parts.fragment))


def _schema_applied(admin_url: str, target: str, *, expected_data_dir: Path | None = None) -> bool:
    """True if `target` DB has the schema fully applied — `schema_migrations` is
    the last table created by db/schema.sql, so its presence means the apply did
    not fail partway."""

    from shared.pg_admin import connect

    with connect(_swap_db(admin_url, target), expected_data_dir=expected_data_dir) as conn:
        row = conn.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name = 'schema_migrations'"
        ).fetchone()
    return row is not None


def ensure_cluster_role(
    identity: str,
    *,
    base_admin_url: str,
    expected_data_dir: Path | None = None,
) -> None:
    """Create the cluster's schema owner `identity` as NOLOGIN (no password) and
    make it own the database of the same name. Idempotent — safe on every bring-up.

    `identity` is names-as-data: the caller reads it from the cluster's own
    db_url (`identity_from_url`) for an existing cluster, or passes
    `DATA_PLANE_IDENTITY` at birth — it is never derived from a cluster name, so
    prod's historical `ava_main` keeps working until an ops rename.

    The owner never logs in: every DDL path is the administrator acting as it
    (`owner_session`), and application processes hold write-generation logins.
    An existing role is never given LOGIN or a password here; demoting a legacy
    login owner belongs to birth/cutover authority
    (`shared.cluster.authority.retire_legacy_logins`). The initdb bootstrap
    superuser (the installing OS user) cannot be an owner that must lose LOGIN,
    so an identity equal to it refuses. When the database already exists,
    ownership is adopted so migrations acting as the owner own their objects.

    base_admin_url must connect as the bootstrap superuser over the owner-only
    socket to a maintenance db (e.g. `postgres`) on the same instance.

    Raises:
        RuntimeError: `identity` is the bootstrap superuser.
    """
    from psycopg import sql as pgsql

    from shared.pg_admin import connect

    with connect(base_admin_url, expected_data_dir=expected_data_dir, autocommit=True) as conn:
        row = conn.execute("SELECT oid FROM pg_roles WHERE rolname = %s", (identity,)).fetchone()
        if row is not None and row[0] == 10:  # BOOTSTRAP_SUPERUSER_OID
            raise RuntimeError(
                f"schema owner {identity!r} is the initdb bootstrap superuser; the owner "
                "must be a NOLOGIN role distinct from the OS user"
            )
        if row is None:
            conn.execute(
                pgsql.SQL(
                    "CREATE ROLE {} NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE"
                    " NOREPLICATION NOBYPASSRLS"
                ).format(pgsql.Identifier(identity))
            )
        db_exists = conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (identity,)
        ).fetchone()
    if db_exists:
        _adopt_database(base_admin_url, identity, identity, expected_data_dir=expected_data_dir)


def _adopt_database(
    base_admin_url: str,
    target: str,
    owner: str,
    *,
    expected_data_dir: Path | None = None,
) -> None:
    """Make `owner` own database `target`. Idempotent — safe on every bring-up.

    The legacy single shared `ava` role's REASSIGN OWNED migration is retired
    (2026-09-20, batch b5): no live instance still needs it, and the fleet's
    target databases carry no `ava`-owned objects (verified per host)."""
    from psycopg import sql as pgsql

    from shared.pg_admin import connect

    with connect(base_admin_url, expected_data_dir=expected_data_dir, autocommit=True) as conn:
        conn.execute(
            pgsql.SQL("ALTER DATABASE {} OWNER TO {}").format(
                pgsql.Identifier(target), pgsql.Identifier(owner)
            )
        )


def provision_database(
    identity: str,
    *,
    base_admin_url: str,
    resume_initialization: bool = False,
    expected_data_dir: Path | None = None,
) -> bool:
    """Atomically provision a cluster's Postgres database AND its owning role:
    ensure the NOLOGIN owner `identity`, CREATE DATABASE `identity` OWNED BY it,
    and apply db/schema.sql acting as that role (`owner_session`) so every object
    is role-owned. `identity` is the shared db/role identifier (names-as-data —
    `DATA_PLANE_IDENTITY` at birth). base_admin_url must connect as the
    bootstrap superuser to a maintenance db (`postgres`) on the same Postgres.

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
    from psycopg import sql as pgsql

    from shared.pg_admin import connect, owner_session

    ensure_cluster_role(
        identity, base_admin_url=base_admin_url, expected_data_dir=expected_data_dir
    )
    with connect(base_admin_url, expected_data_dir=expected_data_dir, autocommit=True) as conn:
        exists = conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (identity,)
        ).fetchone()
    if exists:
        if _schema_applied(base_admin_url, identity, expected_data_dir=expected_data_dir):
            return False  # role already re-affirmed and ownership adopted
        if not resume_initialization:
            raise RuntimeError(
                f"database {identity!r} has an incomplete schema without initialization authority"
            )
        # A durable fresh-home intent owns this incomplete baseline. SQL executes
        # transactionally; resume an empty database, never delete unknown objects.
        with connect(
            _swap_db(base_admin_url, identity), expected_data_dir=expected_data_dir
        ) as conn:
            row = conn.execute(
                "SELECT count(*) FROM pg_tables WHERE schemaname = 'public'"
            ).fetchone()
        if row is None or row[0] != 0:
            raise RuntimeError("incomplete initialization contains unknown database objects")

    if not exists:
        with connect(base_admin_url, expected_data_dir=expected_data_dir, autocommit=True) as conn:
            # db and role share the identifier; sql.Identifier quotes it.
            conn.execute(
                pgsql.SQL("CREATE DATABASE {} OWNER {}").format(
                    pgsql.Identifier(identity), pgsql.Identifier(identity)
                )
            )
    schema_sql = (Path(__file__).resolve().parents[2] / "db" / "schema.sql").read_text()
    try:
        # Apply the schema as the administrator ACTING AS the cluster role, so
        # every object is owned by the role, not the bootstrap superuser — the
        # role must own them for later migrations and its grants. The role never
        # logs in here. schema.sql is a trusted multi-statement script read from
        # disk; same pattern as shared/migrations.py applying a body.
        with owner_session(
            base_admin_url,
            database=identity,
            owner=identity,
            expected_data_dir=expected_data_dir,
            autocommit=True,
        ) as conn:
            conn.execute(schema_sql)  # type: ignore[arg-type]
    except Exception:
        # Drop the half-built DB so the next provision attempt starts clean
        # rather than tripping the "exists but incomplete" guard above.
        if not exists:
            drop_database(
                identity, base_admin_url=base_admin_url, expected_data_dir=expected_data_dir
            )
        raise
    return not bool(exists)


def ensure_pgvector_extension(
    identity: str, *, base_admin_url: str, expected_data_dir: Path | None = None
) -> None:
    """Pre-create the pgvector extension in the cluster database with the
    bootstrap-superuser connection (`base_admin_url`), so the application
    logins never need to: pgvector's `vector.control` ships without
    `trusted = true`, which makes `CREATE EXTENSION` superuser-only by
    Postgres' own policy (deliberately not overridden on the injected control
    file). Idempotent — normal gateway start prepares it before the pgvector
    memory table, so a cluster picks the extension up on its next start.

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

    from shared.pg_admin import connect

    try:
        with connect(base_admin_url, expected_data_dir=expected_data_dir, autocommit=True) as conn:
            available = conn.execute(
                "SELECT 1 FROM pg_available_extensions WHERE name = 'vector'"
            ).fetchone()
        if available is None:
            return
        with connect(
            _swap_db(base_admin_url, identity), expected_data_dir=expected_data_dir, autocommit=True
        ) as conn:
            conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
    except psycopg.OperationalError as exc:
        logger.warning(
            "[pgvector] pre-create skipped: cluster Postgres unreachable over the "
            "admin connection (%s) — re-attempted on the next bring-up",
            exc,
        )


def drop_database(
    identity: str, *, base_admin_url: str, expected_data_dir: Path | None = None
) -> None:
    """DROP DATABASE `identity` + its owning role of the same name — the inverse
    of provision_database, and its half-built rollback. base_admin_url must
    connect as the bootstrap superuser (owner-only socket, `peer`) to a
    maintenance db (`postgres`) on the same Postgres.

    Idempotent (IF EXISTS). The caller is responsible for there being no live
    connections to the target (in prod `ava cluster destroy` runs after the
    cluster is stopped); this does not force-terminate backends. The role is
    dropped after the database so it owns nothing and the drop cannot fail on a
    dependency.
    """
    from psycopg import sql as pgsql

    from shared.pg_admin import connect

    with connect(base_admin_url, expected_data_dir=expected_data_dir, autocommit=True) as conn:
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


def _checkpoint_schema_versions(
    db_url: str, *, expected_data_dir: Path | None = None
) -> frozenset[int] | None:
    """Return the complete applied set, or ``None`` when no schema exists."""

    from shared.pg_admin import connect

    with connect(db_url, expected_data_dir=expected_data_dir, autocommit=True) as conn:
        table = conn.execute("SELECT to_regclass('public.checkpoint_migrations')").fetchone()
        if table is None or table[0] is None:
            return None
        rows = conn.execute("SELECT v FROM checkpoint_migrations").fetchall()
    return frozenset(int(row[0]) for row in rows)


def assert_checkpoint_schema_current(db_url: str, *, expected_data_dir: Path | None = None) -> None:
    """Require the exact approved checkpoint migration set, without mutation.

    Every start role calls this after Ava migrations.  A pure runner therefore
    detects behind, ahead, empty, or internally-gapped checkpoint state without
    acquiring DDL capability.  Ahead is also refused: upstream gives no old-
    runtime/new-schema compatibility guarantee, while Ava rollback can safely
    reverse only schema changes represented by paired Ava migrations.
    """
    expected = _expected_checkpoint_schema_versions()
    actual = _checkpoint_schema_versions(db_url, expected_data_dir=expected_data_dir)
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
    database_created: bool = False,
    resume_partial: bool = False,
    expected_data_dir: Path | None = None,
) -> None:
    """Create the LangGraph checkpoint tables (idempotent) AS the cluster role.

    Runs `PostgresSaver.setup()` acting as the cluster's schema owner
    (`owner_session`), so the owner owns the tables and the capability groups
    reach them through grants. Called during first-start initialization BEFORE
    the group grants (`shared.cluster.authority.ensure_groups`): table grants
    can only target existing tables, and an application login never runs
    setup(): Postgres refuses `CREATE TABLE IF NOT EXISTS` for a role without
    CREATE on the schema even when the tables exist (no group holds CREATE, by
    design — any DDL must fail under an application login).

    Setup requires fresh initialization authority. ``database_created`` is the exact result of this
    birth's ``provision_database`` call, not registry state. Upstream setup is
    autocommit, so a failure can leave a contiguous prefix; when this call owns
    the newly-created DB it drops that DB and role, making retry start clean.
    ``resume_partial`` is separate, explicit first-start authority: a hard
    process death cannot run cleanup, so an idempotent birth retry may continue
    only an exact contiguous prefix. Existing cluster/operator paths leave it
    off and never repair, resume, or drop a partial/older/newer schema. Gaps and
    unknown versions are never resumable.
    """
    from langgraph.checkpoint.postgres import PostgresSaver

    from shared.pg_admin import owner_conninfo, owner_session

    # Act as the schema owner and retain the validated native connection
    # throughout setup; PostgresSaver must not open an unchecked second dial.
    owner_dsn = owner_conninfo(base_admin_url, database=identity, owner=identity)
    expected = _expected_checkpoint_schema_versions()
    actual = _checkpoint_schema_versions(owner_dsn, expected_data_dir=expected_data_dir)
    if actual == expected:
        return
    may_setup = database_created or resume_partial
    resumable = actual is None or actual == frozenset(range(len(actual)))
    if not may_setup or not resumable:
        assert_checkpoint_schema_current(owner_dsn, expected_data_dir=expected_data_dir)
        return
    try:
        from psycopg.rows import dict_row

        with owner_session(
            base_admin_url,
            database=identity,
            owner=identity,
            expected_data_dir=expected_data_dir,
            autocommit=True,
            prepare_threshold=0,
            row_factory=dict_row,
        ) as conn:
            PostgresSaver(conn).setup()
        assert_checkpoint_schema_current(owner_dsn, expected_data_dir=expected_data_dir)
    except Exception:
        if database_created:
            drop_database(
                identity, base_admin_url=base_admin_url, expected_data_dir=expected_data_dir
            )
        raise
