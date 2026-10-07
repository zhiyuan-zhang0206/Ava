"""Contract tests for the `ava_runner` capability group's matrix (Task #1236).

Prove the design's grant matrix on a throwaway Postgres, exercised through a
write-generation-shaped runner login that inherits the NOLOGIN `ava_runner`
group: it can write exactly its audited surface — checkpoint tables (full
CRUD), inbound claim (SELECT/UPDATE), agents_meta status (SELECT/UPDATE),
machine_units (INSERT/UPDATE/SELECT), the compact-boundary enqueue, and SELECT
everywhere — and NOTHING else: agents INSERT, agents_meta INSERT and any DDL
fail with a permission error. The grants come from
`base.cluster.authority.ensure_groups` (the start-path refresh after
migrations). Also covers the checkpoint-schema ensure that makes a fresh
birth's grants target existing tables.
"""

from __future__ import annotations

from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import psycopg
import pytest
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg import sql

from base.cluster import (
    drop_database,
    ensure_checkpoint_schema,
    provision_database,
)
from base.cluster.authority import GATEWAY_GROUP, RUNNER_GROUP, Groups, ensure_groups
from base.cluster.dataplane.pg_tools import throwaway_postgres
from base.db.pg_admin import owner_conninfo
from base.host.net.url_secret import url_with_userinfo
from base.telemetry.metrics.observed_metrics import MetricObservation, write_observations

_RUNNER_PW = "runner-pw-fixture"
_RUNNER_LOGIN = "ava_g0_runner"
_CLUSTER_SECRET = "cluster-secret-x"  # noqa: S105 — test fixture, not a real credential
_IDENTITY = "ava_citest"  # the throwaway db/role the fixture births


def _schema_sql() -> str:
    root = Path(__file__).resolve().parents[2]
    return (root / "db" / "schema.sql").read_text()


@pytest.fixture()
def runner_db() -> Generator[str, None, None]:
    """A throwaway Postgres with schema.sql + checkpoint tables applied."""
    with throwaway_postgres(schema_sql=_schema_sql()) as url:
        yield url


def _admin_url(url: str) -> str:
    """The maintenance-db admin URL (postgres) beside the fixture's db URL."""
    return url.rsplit("/", 1)[0] + "/postgres"


def _runner_url(url: str) -> str:
    return url_with_userinfo(url, _RUNNER_LOGIN, _RUNNER_PW)


def _grant_runner(url: str, identity: str = _IDENTITY) -> None:
    """The start-path grant refresh (`ensure_groups` after migrations) on the
    `identity` database, plus one runner login inheriting the group the way a
    write generation's login does (INHERIT TRUE, SET FALSE, ADMIN FALSE)."""
    groups = Groups(gateway=GATEWAY_GROUP, runner=RUNNER_GROUP)
    with psycopg.connect(url.rsplit("/", 1)[0] + "/" + identity, autocommit=True) as conn:
        ensure_groups(conn, owner=identity, database=identity, groups=groups)
        if conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (_RUNNER_LOGIN,)).fetchone():
            return
        conn.execute(
            sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                sql.Identifier(_RUNNER_LOGIN), sql.Literal(_RUNNER_PW)
            )
        )
        conn.execute(
            sql.SQL("GRANT {} TO {} WITH INHERIT TRUE, SET FALSE, ADMIN FALSE").format(
                sql.Identifier(RUNNER_GROUP), sql.Identifier(_RUNNER_LOGIN)
            )
        )


def test_runner_group_is_a_nologin_capability_and_the_refresh_is_idempotent(
    runner_db: str,
) -> None:
    """The refresh creates `ava_runner` as a NOLOGIN group; running it again is
    a no-op, not an error, and the login's privileges are the group's."""
    _grant_runner(runner_db)
    _grant_runner(runner_db)

    with psycopg.connect(_admin_url(runner_db), autocommit=True) as conn:
        attrs = conn.execute(
            "SELECT rolcanlogin, rolsuper, rolcreatedb, rolcreaterole"
            " FROM pg_roles WHERE rolname = 'ava_runner'"
        ).fetchone()
    assert attrs == (False, False, False, False), "ava_runner is a NOLOGIN capability group"


def test_runner_projects_observations_and_records_actual_lifecycle(runner_db: str) -> None:
    _grant_runner(runner_db)
    with psycopg.connect(runner_db, autocommit=True) as conn:
        conn.execute("INSERT INTO agents (id) VALUES (712345)")
        conn.execute("INSERT INTO agents_meta (id,status) VALUES (712345,'running')")
    with psycopg.connect(_runner_url(runner_db), autocommit=True) as conn:
        fact = MetricObservation(
            event_id=1,
            agent_id=712345,
            occurred_at=datetime.now(UTC),
            kind="usage",
            usage_calls=1,
        )
        assert write_observations([fact], db=conn) == 1
        assert write_observations([fact], db=conn) == 0
        conn.execute("UPDATE agents_meta SET status='terminated' WHERE id=712345")
        conn.execute("UPDATE agents_meta SET status='idling' WHERE id=712345")
        assert conn.execute(
            "SELECT count(*), count(*) FILTER (WHERE ended_at IS NULL) "
            "FROM agent_lifecycle_intervals WHERE agent_id=712345"
        ).fetchone() == (2, 1)
        conn.execute(
            "INSERT INTO agent_metric_file_cursors (source_key, identity, position) "
            "VALUES ('machine/path', '1:2', 100)"
        )
        conn.execute("UPDATE agent_metric_file_cursors SET position=200")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("DELETE FROM agent_metric_observations")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("UPDATE agent_metric_collection SET started_at=now()")


def test_ensure_checkpoint_schema_creates_tables_owned_by_identity(runner_db: str) -> None:
    """ensure_checkpoint_schema creates the LangGraph tables AS the cluster role
    (so the main role keeps its gateway-side checkpoint reads), and the runner
    grants then target existing tables — the fresh-birth order."""
    admin = _admin_url(runner_db)
    identity = "ava_runner_ct2"
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute("CREATE ROLE ava_runner_ct2 LOGIN")
        conn.execute("CREATE DATABASE ava_runner_ct2 OWNER ava_runner_ct2")
    try:
        # Mirror the birth order: schema.sql applied AS the cluster role first
        # (provision_database does exactly this), then the checkpoint-schema
        # ensure — the checkpoint tables are the only ones not in schema.sql.
        with psycopg.connect(
            url_with_userinfo(
                runner_db.rsplit("/", 1)[0] + "/" + identity, identity, _CLUSTER_SECRET
            ),
            autocommit=True,
        ) as conn:
            conn.execute(_schema_sql())  # type: ignore[arg-type]
        ensure_checkpoint_schema(
            identity,
            base_admin_url=admin,
            database_created=True,
        )
        with psycopg.connect(
            url_with_userinfo(
                runner_db.rsplit("/", 1)[0] + "/" + identity, identity, _CLUSTER_SECRET
            ),
            autocommit=True,
        ) as conn:
            for table in (
                "checkpoint_migrations",
                "checkpoints",
                "checkpoint_blobs",
                "checkpoint_writes",
            ):
                owner = conn.execute(
                    "SELECT pg_get_userbyid(relowner) FROM pg_class WHERE relname = %s", (table,)
                ).fetchone()
                assert owner == (identity,), f"{table} must be owned by {identity}"

        # The runner grants now target the freshly-created tables — the birth order
        # a cluster's provisioning uses (checkpoint schema first, grants second).
        _grant_runner(runner_db, identity)
        with psycopg.connect(
            url_with_userinfo(
                runner_db.rsplit("/", 1)[0] + "/" + identity, _RUNNER_LOGIN, _RUNNER_PW
            ),
            autocommit=True,
        ) as conn:
            conn.execute(
                "INSERT INTO checkpoints (thread_id, checkpoint_id, checkpoint, metadata)"
                " VALUES ('t1', 'c1', '{}'::jsonb, '{}'::jsonb)"
            )
    finally:
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity"
                " WHERE datname = 'ava_runner_ct2' AND pid <> pg_backend_pid()"
            )
            conn.execute("DROP DATABASE IF EXISTS ava_runner_ct2")
            conn.execute("DROP ROLE IF EXISTS ava_runner_ct2")


def test_fresh_install_dependency_drift_precedes_checkpoint_setup(
    runner_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A new upstream migration cannot mutate even a fresh cluster by surprise."""
    from base.cluster.provision import CheckpointDependencyDriftError

    admin = _admin_url(runner_db)
    identity = "ava_runner_ct3"
    db_url = url_with_userinfo(
        runner_db.rsplit("/", 1)[0] + "/" + identity, identity, _CLUSTER_SECRET
    )
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute("CREATE ROLE ava_runner_ct3 LOGIN")
        conn.execute("CREATE DATABASE ava_runner_ct3 OWNER ava_runner_ct3")
    try:
        with psycopg.connect(db_url, autocommit=True) as conn:
            conn.execute(_schema_sql())  # type: ignore[arg-type]

        monkeypatch.setattr(PostgresSaver, "MIGRATIONS", [*PostgresSaver.MIGRATIONS, "SELECT 1"])
        with pytest.raises(CheckpointDependencyDriftError, match="Ava timestamp migration"):
            ensure_checkpoint_schema(identity, base_admin_url=admin)

        with psycopg.connect(db_url, autocommit=True) as conn:
            row = conn.execute("SELECT to_regclass('public.checkpoint_migrations')").fetchone()
        assert row == (None,)
    finally:
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity"
                " WHERE datname = 'ava_runner_ct3' AND pid <> pg_backend_pid()"
            )
            conn.execute("DROP DATABASE IF EXISTS ava_runner_ct3")
            conn.execute("DROP ROLE IF EXISTS ava_runner_ct3")


def test_default_missing_schema_refuses_setup(
    runner_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Operator/default paths cannot turn a missing table into DDL authority."""
    from base.cluster.provision import CheckpointSchemaMismatchError

    with psycopg.connect(runner_db, autocommit=True) as conn:
        conn.execute("DROP TABLE checkpoint_migrations")

    def setup_must_not_run(_self: PostgresSaver) -> None:
        raise AssertionError("missing schema without birth authority must not run setup")

    monkeypatch.setattr(PostgresSaver, "setup", setup_must_not_run)
    with pytest.raises(CheckpointSchemaMismatchError):
        ensure_checkpoint_schema(_IDENTITY, base_admin_url=_admin_url(runner_db))

    with psycopg.connect(runner_db, autocommit=True) as conn:
        row = conn.execute("SELECT to_regclass('public.checkpoint_migrations')").fetchone()
    assert row == (None,)


def test_new_database_setup_failure_is_dropped_then_retry_converges(
    runner_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real PG: caught autocommit setup failure leaves no half-born database."""
    admin = _admin_url(runner_db)
    identity = "ava_runner_ct4"
    # The provisioned owner is NOLOGIN: the administrator acts as it.
    db_url = owner_conninfo(admin, database=identity, owner=identity)
    original_setup = PostgresSaver.setup

    assert provision_database(identity, base_admin_url=admin) is True

    def fail_after_v0(_self: PostgresSaver) -> None:
        with psycopg.connect(db_url, autocommit=True) as conn:
            conn.execute(PostgresSaver.MIGRATIONS[0])  # pyright: ignore[reportCallIssue, reportArgumentType]
            conn.execute("INSERT INTO checkpoint_migrations (v) VALUES (0)")
        raise RuntimeError("injected setup crash after v0")

    monkeypatch.setattr(PostgresSaver, "setup", fail_after_v0)
    with pytest.raises(RuntimeError, match="injected setup crash"):
        ensure_checkpoint_schema(
            identity,
            base_admin_url=admin,
            database_created=True,
        )
    with psycopg.connect(admin, autocommit=True) as conn:
        row = conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (identity,)).fetchone()
    assert row is None

    monkeypatch.setattr(PostgresSaver, "setup", original_setup)
    try:
        assert provision_database(identity, base_admin_url=admin) is True
        ensure_checkpoint_schema(
            identity,
            base_admin_url=admin,
            database_created=True,
        )
        with psycopg.connect(db_url, autocommit=True) as conn:
            rows = conn.execute("SELECT v FROM checkpoint_migrations").fetchall()
        assert {row[0] for row in rows} == set(range(10))
    finally:
        drop_database(identity, base_admin_url=admin)


def test_checkpoint_reads_need_crud_not_schema_ddl(
    runner_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both checkpoint readers work as ``ava_runner`` without schema CREATE.

    The checkpoint schema is an install / migration concern.  A reader calling
    ``PostgresSaver.setup()`` first issues ``CREATE TABLE IF NOT EXISTS`` and
    PostgreSQL correctly rejects it for this least-privilege runtime role even
    when every table already exists.  Production then loses timeline / inspect
    history despite the role holding all CRUD privileges the read itself needs.
    """
    from langchain_core.messages import HumanMessage
    from langgraph.checkpoint.base import CheckpointMetadata, empty_checkpoint

    from base.agents.history.checkpoint import (
        load_checkpoint_messages,
        load_checkpoint_messages_by_trace,
    )
    from base.config import settings
    from base.db import Database

    _grant_runner(runner_db)

    trace_id = "f" * 32
    checkpoint = empty_checkpoint()
    checkpoint["channel_values"] = {"messages": [HumanMessage(content="runtime read")]}
    checkpoint["channel_versions"] = {"messages": "1", "__start__": "1"}
    with PostgresSaver.from_conn_string(runner_db) as saver:
        saved = saver.put(
            config={"configurable": {"thread_id": "73", "checkpoint_ns": ""}},
            checkpoint=checkpoint,
            metadata=cast(
                CheckpointMetadata,
                {"source": "input", "step": 1, "parents": {}, "trace_id": trace_id},
            ),
            new_versions={"messages": "1"},
        )

    monkeypatch.setattr(settings.data_plane, "db_url", _runner_url(runner_db))
    current = load_checkpoint_messages(Database.from_settings(), 73)
    checkpoint_id, traced = load_checkpoint_messages_by_trace(
        Database.from_settings(), 73, trace_id
    )

    assert current == [HumanMessage(content="runtime read")]
    assert checkpoint_id == saved["configurable"]["checkpoint_id"]  # pyright: ignore[reportTypedDictNotRequiredAccess]
    assert traced == [HumanMessage(content="runtime read")]


def test_current_checkpoint_schema_skips_setup(
    runner_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-running install provisioning on a current schema performs no DDL."""

    def setup_must_not_run(_self: PostgresSaver) -> None:
        raise AssertionError("current checkpoint schema must bypass setup")

    monkeypatch.setattr(PostgresSaver, "setup", setup_must_not_run)
    ensure_checkpoint_schema(_IDENTITY, base_admin_url=_admin_url(runner_db))


@pytest.mark.parametrize(
    "corruption_sql",
    [
        "DROP TABLE checkpoint_migrations",
        "DELETE FROM checkpoint_migrations",
        "DELETE FROM checkpoint_migrations WHERE v = 9",
        "DELETE FROM checkpoint_migrations WHERE v = 4",
        "INSERT INTO checkpoint_migrations (v) VALUES (10)",
        "INSERT INTO checkpoint_migrations (v) VALUES (-1)",
    ],
    ids=["table-missing", "empty", "behind", "internal-gap", "ahead", "unknown"],
)
def test_runner_checkpoint_schema_assertion_requires_exact_set(
    runner_db: str, corruption_sql: str
) -> None:
    """The CRUD-only runner detects every schema drift shape without setup."""
    from base.cluster import assert_checkpoint_schema_current
    from base.cluster.provision import CheckpointSchemaMismatchError

    _grant_runner(runner_db)
    with psycopg.connect(runner_db, autocommit=True) as conn:
        conn.execute(corruption_sql)  # type: ignore[arg-type]

    with pytest.raises(CheckpointSchemaMismatchError):
        assert_checkpoint_schema_current(_runner_url(runner_db))


def test_runner_accepts_exact_checkpoint_schema_read_only(runner_db: str) -> None:
    """A pure runner can pass the start gate with checkpoint SELECT alone."""
    from base.cluster import assert_checkpoint_schema_current

    _grant_runner(runner_db)
    assert_checkpoint_schema_current(_runner_url(runner_db))


def test_existing_behind_schema_never_falls_back_to_setup(
    runner_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only an absent fresh-install schema may invoke upstream setup."""
    from base.cluster.provision import CheckpointSchemaMismatchError

    with psycopg.connect(runner_db, autocommit=True) as conn:
        conn.execute("DELETE FROM checkpoint_migrations WHERE v = 9")

    def setup_must_not_run(_self: PostgresSaver) -> None:
        raise AssertionError("existing schemas must use Ava migrations")

    monkeypatch.setattr(PostgresSaver, "setup", setup_must_not_run)
    with pytest.raises(CheckpointSchemaMismatchError):
        ensure_checkpoint_schema(_IDENTITY, base_admin_url=_admin_url(runner_db))


def test_birth_retry_resumes_contiguous_prefix_after_setup_crash(
    runner_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real PG: a hard-crash-shaped partial birth converges on install retry."""
    with psycopg.connect(runner_db, autocommit=True) as conn:
        conn.execute("DELETE FROM checkpoint_migrations")

    original_setup = PostgresSaver.setup

    def fail_after_v0(_self: PostgresSaver) -> None:
        with psycopg.connect(runner_db, autocommit=True) as conn:
            conn.execute("INSERT INTO checkpoint_migrations (v) VALUES (0)")
        raise RuntimeError("injected setup crash after v0")

    monkeypatch.setattr(PostgresSaver, "setup", fail_after_v0)
    with pytest.raises(RuntimeError, match="injected setup crash"):
        ensure_checkpoint_schema(
            _IDENTITY,
            base_admin_url=_admin_url(runner_db),
            resume_partial=True,
        )

    with psycopg.connect(runner_db, autocommit=True) as conn:
        rows = conn.execute("SELECT v FROM checkpoint_migrations").fetchall()
    assert rows == [(0,)]

    monkeypatch.setattr(PostgresSaver, "setup", original_setup)
    ensure_checkpoint_schema(
        _IDENTITY,
        base_admin_url=_admin_url(runner_db),
        resume_partial=True,
    )
    with psycopg.connect(runner_db, autocommit=True) as conn:
        rows = conn.execute("SELECT v FROM checkpoint_migrations").fetchall()
    assert {row[0] for row in rows} == set(range(10))


def test_birth_retry_refuses_non_prefix_checkpoint_state(
    runner_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Install repair authority never blesses a gap as a resumable prefix."""
    from base.cluster.provision import CheckpointSchemaMismatchError

    with psycopg.connect(runner_db, autocommit=True) as conn:
        conn.execute("DELETE FROM checkpoint_migrations WHERE v = 4")

    def setup_must_not_run(_self: PostgresSaver) -> None:
        raise AssertionError("gapped checkpoint state must not be repaired")

    monkeypatch.setattr(PostgresSaver, "setup", setup_must_not_run)
    with pytest.raises(CheckpointSchemaMismatchError):
        ensure_checkpoint_schema(
            _IDENTITY,
            base_admin_url=_admin_url(runner_db),
            resume_partial=True,
        )


def _exercise_pause_grants(conn: psycopg.Connection, agent_id: int) -> None:
    """The pause-trail surface ava.self.pause_heartbeat writes from the runner
    process: SELECT the previous window (the backoff-reminder lookup) and
    INSERT the new row; the BIGSERIAL id draws from the owning sequence.
    Regression for task #1932: the table shipped without a runner grant and
    every INSERT failed with InsufficientPrivilege until prod was patched by
    hand. UPDATE/DELETE stay ungranted — the trail is append-only.
    """
    conn.execute(
        "INSERT INTO heartbeat_pause_log (agent_id, duration_s) VALUES (%s, 1800)",
        (agent_id,),
    )
    row = conn.execute(
        "SELECT duration_s FROM heartbeat_pause_log"
        " WHERE agent_id = %s ORDER BY created_at DESC, id DESC LIMIT 1",
        (agent_id,),
    ).fetchone()
    assert row == (1800,)


def _exercise_impersonation_entry_grants(conn: psycopg.Connection, agent_id: int) -> None:
    """The named-impersonation session trail the lease lifecycle writes from
    the database side: creating a lease fires the lifecycle trigger, whose
    INSERT into agent_impersonation_entries must land, and the relay reads
    rows back by lease.
    Regression for task #3549: the table shipped without a runner grant and
    lease creation failed with InsufficientPrivilege. UPDATE/DELETE stay
    ungranted — the trail is append-only (the preserve trigger rejects
    rewrites).
    """
    lease = conn.execute(
        "INSERT INTO agent_impersonations (id, agent_id, source, machine,"
        " status, ttl_seconds, expires_at)"
        " VALUES (gen_random_uuid(), %s, 'codex', 'test-machine', 'requested',"
        " 3600, now() + interval '1 hour') RETURNING id",
        (agent_id,),
    ).fetchone()
    assert lease is not None
    row = conn.execute(
        "SELECT count(*) FROM agent_impersonation_entries WHERE lease_id = %s", (lease[0],)
    ).fetchone()
    assert row == (1,)
    # The session allocator bumps agents.impersonation_index on the runner's
    # behalf (SECURITY DEFINER), though the runner holds no UPDATE on agents.
    allocated = conn.execute(
        "SELECT a.session_id, g.impersonation_index FROM agent_impersonations a"
        " JOIN agents g ON g.id = a.agent_id WHERE a.id = %s",
        (lease[0],),
    ).fetchone()
    assert allocated == (0, 1)


def _exercise_understanding_node_grants(conn: psycopg.Connection, agent_id: int) -> None:
    """The understanding-node surface the understanding loops write from the
    runner side (manual first-run / ad-hoc regeneration, task #3704): INSERT a
    node (the BIGSERIAL id draws from the owning sequence), UPDATE it in
    place, SELECT it back, and DELETE it — the reconciliation removes rows of
    a superseded earlier cut (the provisional tail re-splits as history grows;
    sealed cells are never touched).
    """
    conn.execute(
        "INSERT INTO understanding_nodes (agent_id, depth, span_start, span_end,"
        " segment_key, text, text_hash, input_hash, children_count, model,"
        " engine_version, prompt_version, schema_version)"
        " VALUES (%s, 1, 0, 9, 'compact@i9', 'leaf text', 'th', 'ih', 0,"
        " 'deepseek-v4-flash', '0.3', '0.3', 1)",
        (agent_id,),
    )
    conn.execute(
        "UPDATE understanding_nodes SET text = 'rewritten', text_hash = 'th2'"
        " WHERE agent_id = %s AND depth = 1 AND span_start = 0 AND span_end = 9",
        (agent_id,),
    )
    row = conn.execute(
        "SELECT text FROM understanding_nodes WHERE agent_id = %s"
        " AND depth = 1 AND span_start = 0 AND span_end = 9",
        (agent_id,),
    ).fetchone()
    assert row == ("rewritten",)
    conn.execute(
        "DELETE FROM understanding_nodes WHERE agent_id = %s"
        " AND depth = 1 AND span_start = 0 AND span_end = 9",
        (agent_id,),
    )
    assert (
        conn.execute(
            "SELECT 1 FROM understanding_nodes WHERE agent_id = %s", (agent_id,)
        ).fetchone()
        is None
    )


def _exercise_understanding_queue_grants(conn: psycopg.Connection, agent_id: int) -> None:
    """The chunk-triggered understanding queue the runner process writes: INSERT a
    pending job (BIGSERIAL id from the owning sequence), claim it with an UPDATE and
    SELECT it back."""
    conn.execute(
        "INSERT INTO understanding_chunk_jobs"
        " (agent_id, compact_version, start_index, end_index, end_msg_id)"
        " VALUES (%s, 0, 8, 20, 'm20')",
        (agent_id,),
    )
    conn.execute(
        "UPDATE understanding_chunk_jobs SET status = 'running', attempts = attempts + 1"
        " WHERE agent_id = %s",
        (agent_id,),
    )
    row = conn.execute(
        "SELECT status FROM understanding_chunk_jobs WHERE agent_id = %s", (agent_id,)
    ).fetchone()
    assert row == ("running",)


def _assert_alert_writes_denied(conn: psycopg.Connection) -> None:
    """The alerts table is written only by the gateway ingest: the runner reads
    it and holds neither INSERT nor UPDATE (nor DELETE)."""
    assert conn.execute("SELECT count(*) FROM alerts").fetchone() == (0,)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute(
            "INSERT INTO alerts (status, severity, alertname, labels, starts_at,"
            " fingerprint, source) VALUES ('unresolved', 'warning', 'x', '{}'::jsonb,"
            " now(), 'fp-runner', 'grafana')"
        )
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute("UPDATE alerts SET status = 'resolved' WHERE fingerprint = 'fp-runner'")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute("DELETE FROM alerts WHERE fingerprint = 'fp-runner'")


def _identity_url(url: str) -> str:
    """The cluster's MAIN identity — the role migrations actually run as.
    `AVA_DB_URL` names role and database with one identifier on a live cluster
    (`identity_from_url`); the fixture dials the initdb superuser instead,
    making a default-privileges test pass for the wrong reason — default
    privileges key on the role that CREATES the object.
    """
    return url.replace("://ava@", f"://{_IDENTITY}@", 1)
