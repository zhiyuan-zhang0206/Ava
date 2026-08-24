"""Contract tests for the ava_runner least-privilege role (Task #1236).

Prove the design's grant matrix on a throwaway Postgres: the runner role can
write exactly its audited surface — checkpoint tables (full CRUD), inbound
claim (SELECT/UPDATE), agents_meta status (SELECT/UPDATE), machine_units
(INSERT/UPDATE/SELECT), and SELECT everywhere — and NOTHING else: agents
INSERT, agents_meta INSERT and any DDL fail with a permission error. Also
covers idempotent provisioning, the password re-affirm on re-run, and the
checkpoint-schema ensure that makes a fresh birth's grants target existing
tables.
"""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path
from typing import cast

import psycopg
import pytest
from langgraph.checkpoint.postgres import PostgresSaver

from shared.cluster import (
    drop_database,
    ensure_checkpoint_schema,
    ensure_runner_role,
    provision_database,
)
from shared.pg_tools import throwaway_postgres
from shared.url_secret import url_with_userinfo

_RUNNER_PW = "runner-pw-fixture"
_ROTATED_PW = "rotated-pw-fixture"
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
    return url_with_userinfo(url, "ava_runner", _RUNNER_PW)


def test_ensure_runner_role_provisions_idempotently(runner_db: str) -> None:
    """One call creates ava_runner LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE;
    a second call is a no-op, not an error."""
    admin = _admin_url(runner_db)
    ensure_runner_role(_IDENTITY, base_admin_url=admin, runner_password=_RUNNER_PW)
    ensure_runner_role(_IDENTITY, base_admin_url=admin, runner_password=_RUNNER_PW)

    with psycopg.connect(admin, autocommit=True) as conn:
        attrs = conn.execute(
            "SELECT rolcanlogin, rolsuper, rolcreatedb, rolcreaterole"
            " FROM pg_roles WHERE rolname = 'ava_runner'"
        ).fetchone()
    assert attrs == (True, False, False, False), (
        "ava_runner must be LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE"
    )


def test_ensure_runner_role_reauths_password_on_rerun(runner_db: str) -> None:
    """Re-running with a different password rotates the role's stored verifier —
    the .env edit + `ava cluster ensure-db-role` self-heal path."""
    admin = _admin_url(runner_db)
    ensure_runner_role(_IDENTITY, base_admin_url=admin, runner_password=_RUNNER_PW)

    def verifier() -> str:
        with psycopg.connect(admin, autocommit=True) as conn:
            row = conn.execute(
                "SELECT rolpassword FROM pg_authid WHERE rolname = 'ava_runner'"
            ).fetchone()
        assert row is not None
        return row[0]

    before = verifier()
    ensure_runner_role(_IDENTITY, base_admin_url=admin, runner_password=_ROTATED_PW)
    assert verifier() != before


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
            db_admin_password=_CLUSTER_SECRET,
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
        # install_cluster uses (checkpoint schema first, grants second).
        ensure_runner_role(identity, base_admin_url=admin, runner_password=_RUNNER_PW)
        with psycopg.connect(
            url_with_userinfo(
                runner_db.rsplit("/", 1)[0] + "/" + identity, "ava_runner", _RUNNER_PW
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
    from shared.cluster.provision import CheckpointDependencyDriftError

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
        with pytest.raises(CheckpointDependencyDriftError, match="paired Ava timestamp migration"):
            ensure_checkpoint_schema(
                identity, base_admin_url=admin, db_admin_password=_CLUSTER_SECRET
            )

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
    from shared.cluster.provision import CheckpointSchemaMismatchError

    with psycopg.connect(runner_db, autocommit=True) as conn:
        conn.execute("DROP TABLE checkpoint_migrations")

    def setup_must_not_run(_self: PostgresSaver) -> None:
        raise AssertionError("missing schema without birth authority must not run setup")

    monkeypatch.setattr(PostgresSaver, "setup", setup_must_not_run)
    with pytest.raises(CheckpointSchemaMismatchError):
        ensure_checkpoint_schema(
            _IDENTITY, base_admin_url=_admin_url(runner_db), db_admin_password=_CLUSTER_SECRET
        )

    with psycopg.connect(runner_db, autocommit=True) as conn:
        row = conn.execute("SELECT to_regclass('public.checkpoint_migrations')").fetchone()
    assert row == (None,)


def test_default_schema_check_never_setup_after_concurrent_repair(
    runner_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A concurrent repair between both reads cannot grant this call DDL authority."""
    from shared.cluster import provision

    expected = frozenset(range(len(PostgresSaver.MIGRATIONS)))
    observed = iter((expected - {9}, expected))

    def checkpoint_versions(_db_url: str) -> frozenset[int]:
        return next(observed)

    def setup_must_not_run(_self: PostgresSaver) -> None:
        raise AssertionError("a successful read-only recheck must return before setup")

    monkeypatch.setattr(provision, "_checkpoint_schema_versions", checkpoint_versions)
    monkeypatch.setattr(PostgresSaver, "setup", setup_must_not_run)

    ensure_checkpoint_schema(
        _IDENTITY, base_admin_url=_admin_url(runner_db), db_admin_password=_CLUSTER_SECRET
    )
    with pytest.raises(StopIteration):
        next(observed)


def test_new_database_setup_failure_is_dropped_then_retry_converges(
    runner_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real PG: caught autocommit setup failure leaves no half-born database."""
    admin = _admin_url(runner_db)
    identity = "ava_runner_ct4"
    db_url = url_with_userinfo(
        runner_db.rsplit("/", 1)[0] + "/" + identity, identity, _CLUSTER_SECRET
    )
    original_setup = PostgresSaver.setup

    assert (
        provision_database(identity, base_admin_url=admin, db_admin_password=_CLUSTER_SECRET)
        is True
    )

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
            db_admin_password=_CLUSTER_SECRET,
            database_created=True,
        )
    with psycopg.connect(admin, autocommit=True) as conn:
        row = conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (identity,)).fetchone()
    assert row is None

    monkeypatch.setattr(PostgresSaver, "setup", original_setup)
    try:
        assert (
            provision_database(identity, base_admin_url=admin, db_admin_password=_CLUSTER_SECRET)
            is True
        )
        ensure_checkpoint_schema(
            identity,
            base_admin_url=admin,
            db_admin_password=_CLUSTER_SECRET,
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

    from shared.checkpoint import (
        load_checkpoint_messages,
        load_checkpoint_messages_by_trace,
    )
    from shared.config import settings

    ensure_runner_role(_IDENTITY, base_admin_url=_admin_url(runner_db), runner_password=_RUNNER_PW)

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
    current = load_checkpoint_messages(73)
    checkpoint_id, traced = load_checkpoint_messages_by_trace(73, trace_id)

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
    ensure_checkpoint_schema(
        _IDENTITY, base_admin_url=_admin_url(runner_db), db_admin_password=_CLUSTER_SECRET
    )


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
    from shared.cluster import assert_checkpoint_schema_current
    from shared.cluster.provision import CheckpointSchemaMismatchError

    ensure_runner_role(_IDENTITY, base_admin_url=_admin_url(runner_db), runner_password=_RUNNER_PW)
    with psycopg.connect(runner_db, autocommit=True) as conn:
        conn.execute(corruption_sql)  # type: ignore[arg-type]

    with pytest.raises(CheckpointSchemaMismatchError):
        assert_checkpoint_schema_current(_runner_url(runner_db))


def test_runner_accepts_exact_checkpoint_schema_read_only(runner_db: str) -> None:
    """A pure runner can pass the start gate with checkpoint SELECT alone."""
    from shared.cluster import assert_checkpoint_schema_current

    ensure_runner_role(_IDENTITY, base_admin_url=_admin_url(runner_db), runner_password=_RUNNER_PW)
    assert_checkpoint_schema_current(_runner_url(runner_db))


def test_existing_behind_schema_never_falls_back_to_setup(
    runner_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only an absent fresh-install schema may invoke upstream setup."""
    from shared.cluster.provision import CheckpointSchemaMismatchError

    with psycopg.connect(runner_db, autocommit=True) as conn:
        conn.execute("DELETE FROM checkpoint_migrations WHERE v = 9")

    def setup_must_not_run(_self: PostgresSaver) -> None:
        raise AssertionError("existing schemas must use paired Ava migrations")

    monkeypatch.setattr(PostgresSaver, "setup", setup_must_not_run)
    with pytest.raises(CheckpointSchemaMismatchError):
        ensure_checkpoint_schema(
            _IDENTITY, base_admin_url=_admin_url(runner_db), db_admin_password=_CLUSTER_SECRET
        )


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
            db_admin_password=_CLUSTER_SECRET,
            resume_partial=True,
        )

    with psycopg.connect(runner_db, autocommit=True) as conn:
        rows = conn.execute("SELECT v FROM checkpoint_migrations").fetchall()
    assert rows == [(0,)]

    monkeypatch.setattr(PostgresSaver, "setup", original_setup)
    ensure_checkpoint_schema(
        _IDENTITY,
        base_admin_url=_admin_url(runner_db),
        db_admin_password=_CLUSTER_SECRET,
        resume_partial=True,
    )
    with psycopg.connect(runner_db, autocommit=True) as conn:
        rows = conn.execute("SELECT v FROM checkpoint_migrations").fetchall()
    assert {row[0] for row in rows} == set(range(10))


def test_birth_retry_refuses_non_prefix_checkpoint_state(
    runner_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Install repair authority never blesses a gap as a resumable prefix."""
    from shared.cluster.provision import CheckpointSchemaMismatchError

    with psycopg.connect(runner_db, autocommit=True) as conn:
        conn.execute("DELETE FROM checkpoint_migrations WHERE v = 4")

    def setup_must_not_run(_self: PostgresSaver) -> None:
        raise AssertionError("gapped checkpoint state must not be repaired")

    monkeypatch.setattr(PostgresSaver, "setup", setup_must_not_run)
    with pytest.raises(CheckpointSchemaMismatchError):
        ensure_checkpoint_schema(
            _IDENTITY,
            base_admin_url=_admin_url(runner_db),
            db_admin_password=_CLUSTER_SECRET,
            resume_partial=True,
        )


def test_runner_grant_matrix(runner_db: str) -> None:
    """The design's grant matrix, exercised as ava_runner over the wire."""
    admin = _admin_url(runner_db)
    ensure_runner_role(_IDENTITY, base_admin_url=admin, runner_password=_RUNNER_PW)

    # Seed rows as the admin (the gateway side): an agent + its meta + an inbound.
    with psycopg.connect(runner_db, autocommit=True) as conn:
        agent_row = conn.execute(
            "INSERT INTO agents (label) VALUES ('seed') RETURNING id"
        ).fetchone()
        assert agent_row is not None
        agent_id: int = agent_row[0]
        conn.execute(
            "INSERT INTO agents_meta (id, spawner, status) VALUES (%s, 'user', 'idling')",
            (agent_id,),
        )
        inbound_row = conn.execute(
            "INSERT INTO inbound_messages (agent_id, content, source)"
            " VALUES (%s, 'hi', 'system') RETURNING id",
            (agent_id,),
        ).fetchone()
        assert inbound_row is not None
        inbound_id: int = inbound_row[0]
        # An open page row (the runner only UPDATEs pages — INSERT stays with
        # the gateway API's ava.ui.serve/show path)
        conn.execute(
            "INSERT INTO agent_pages (agent_id, name, port) VALUES (%s, 'p', 8000)",
            (agent_id,),
        )

    with psycopg.connect(_runner_url(runner_db), autocommit=True) as conn:
        # ── allowed: the audited runner surface ──
        # read surface: SELECT on every table in public
        assert conn.execute("SELECT * FROM agents").fetchone() is not None
        granted_row = conn.execute(
            "SELECT count(*) FROM information_schema.table_privileges"
            " WHERE grantee = 'ava_runner' AND privilege_type = 'SELECT'"
        ).fetchone()
        assert granted_row is not None
        granted: int = granted_row[0]
        assert granted >= 4, "SELECT must be granted on the seeded tables"

        # inbound claim (SELECT + UPDATE) ...
        conn.execute("UPDATE inbound_messages SET status = 'claimed' WHERE id = %s", (inbound_id,))
        # ... and self-lifecycle inbounds: ava.self.terminate / restart / compact
        # insert their OWN rows from the runner process (NOT via the gateway API)
        # — e2e caught the missing INSERT: a self-terminate whose inbound could
        # not land left the agent 'running' forever.
        conn.execute(
            "INSERT INTO inbound_messages (agent_id, content, kind, source)"
            " VALUES (%s, 'bye', 'terminate', 'self')",
            (agent_id,),
        )
        # agents_meta status/liveness (SELECT + UPDATE; INSERT stays with spawn)
        conn.execute("UPDATE agents_meta SET status = 'idling' WHERE id = %s", (agent_id,))
        # ava.self.set_label writes the agent's OWN agents row (agents INSERT
        # stays denied — spawn-only)
        conn.execute("UPDATE agents SET label = 'me' WHERE id = %s", (agent_id,))
        # SDK write surfaces the runner process uses directly:
        # ava.tasks (INSERT + UPDATE) ...
        conn.execute(
            "INSERT INTO agent_tasks (title, description, created_by) VALUES ('t1', 'd', '123')"
        )
        conn.execute("UPDATE agent_tasks SET status = 'done' WHERE title = 't1'")
        # ava.watcher (INSERT + UPDATE) ...
        conn.execute(
            "INSERT INTO agent_watchers (session_id, agent_id, kind, name)"
            " VALUES (1, %s, 'at', 'w1')",
            (agent_id,),
        )
        conn.execute("UPDATE agent_watchers SET status = 'missed' WHERE agent_id = %s", (agent_id,))
        # ... and the show-page close at exit (agent_pages UPDATE; the row
        # itself was seeded by the gateway side above)
        conn.execute("UPDATE agent_pages SET closed_at = now() WHERE agent_id = %s", (agent_id,))
        # machine_units register_self (INSERT + UPDATE + SELECT)
        conn.execute("INSERT INTO machine_units (machine_name, home) VALUES ('m1', '/h1')")
        conn.execute("UPDATE machine_units SET url = 'http://m1' WHERE machine_name = 'm1'")
        # the runner service chain (prod-deploy finding, #2599 follow-up):
        # `ava start` register_self recomputes the machines row (INSERT ON
        # CONFLICT DO UPDATE) and writes the deploy posture ...
        conn.execute(
            "INSERT INTO machines (name, gateway_url, role, up_since_at)"
            " VALUES ('m1', 'http://gw', ARRAY['agent-runner'], now())"
            " ON CONFLICT (name) DO UPDATE SET up_since_at = EXCLUDED.up_since_at"
        )
        conn.execute(
            "INSERT INTO host_deploy_state (machine, posture, paused_at, updated_at)"
            " VALUES ('m1', 'idle', NULL, now())"
            " ON CONFLICT (machine) DO UPDATE SET posture = EXCLUDED.posture"
        )
        # ... and the ops server dedupes inbound /ops calls
        conn.execute(
            "INSERT INTO api_idempotency (key, method, path, response_body)"
            " VALUES ('k1', 'ops', '/p', '{}'::jsonb)"
        )
        conn.execute("UPDATE api_idempotency SET op_status = 'ok' WHERE key = 'k1'")
        conn.execute("DELETE FROM api_idempotency WHERE key = 'k1'")
        assert (
            conn.execute("SELECT 1 FROM machine_units WHERE machine_name = 'm1'").fetchone()
            is not None
        )
        # checkpoint tables: full CRUD (LangGraph state)
        conn.execute(
            "INSERT INTO checkpoints (thread_id, checkpoint_id, checkpoint, metadata)"
            " VALUES ('t1', 'c1', '{}'::jsonb, '{}'::jsonb)"
        )
        conn.execute("UPDATE checkpoints SET type = 'x' WHERE thread_id = 't1'")
        assert (
            conn.execute("SELECT checkpoint_id FROM checkpoints WHERE thread_id = 't1'").fetchone()
            is not None
        )
        conn.execute(
            "INSERT INTO checkpoint_blobs (thread_id, channel, version, type, blob)"
            " VALUES ('t1', 'ch', 'v1', 'json', NULL)"
        )
        conn.execute(
            "INSERT INTO checkpoint_writes (thread_id, checkpoint_id, task_id, idx,"
            " channel, type, blob) VALUES ('t1', 'c1', 'task', 0, 'ch', 'json', '\\x00'::bytea)"
        )
        conn.execute("DELETE FROM checkpoints WHERE thread_id = 't1'")

        # Agent-boot DDL: PostgresSaver.setup() issues CREATE TABLE IF NOT EXISTS,
        # which Postgres refuses for a role without CREATE on the schema — even on
        # existing tables (verified on PG 17). That refusal is the DESIGNED
        # behavior (any DDL must fail under ava_runner); install + gateway start
        # own setup(), while agent boot and checkpoint reads only use CRUD.
        with (
            pytest.raises(psycopg.errors.InsufficientPrivilege),
            PostgresSaver.from_conn_string(_runner_url(runner_db)) as saver,
        ):
            saver.setup()

        # ── denied: the 2026-08-12 pollution surface ──
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("INSERT INTO agents (label) VALUES ('pollution')")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("INSERT INTO agents_meta (id, spawner) VALUES (999, 'user')")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("CREATE TABLE runner_must_not_ddl (id int)")


def _identity_url(url: str) -> str:
    """The cluster's MAIN identity — the role migrations actually run as.

    On a live cluster `AVA_DB_URL` names role and database with one identifier
    (`identity_from_url`), so the applier connects as `identity`. The throwaway
    fixture's URL dials the initdb superuser instead, which would make a
    default-privileges test pass for the wrong reason: default privileges key on
    the role that CREATES the object.
    """
    return url.replace("://ava@", f"://{_IDENTITY}@", 1)


def test_read_grant_reaches_a_table_created_after_provisioning(runner_db: str) -> None:
    """A table a LATER migration creates is still readable by the runner role.

    `test_runner_grant_matrix` cannot reach this: its fixture applies the whole
    of `schema.sql` and provisions afterwards, so every table it checks existed
    at grant time. The live order is the reverse — install births the cluster and
    grants once, then migrations keep creating tables for the rest of the
    cluster's life.

    That matters because `GRANT SELECT ON ALL TABLES IN SCHEMA public` is a
    point-in-time loop, not a standing policy: Postgres expands it into
    per-object ACL entries and nothing carries forward. Without the
    `ALTER DEFAULT PRIVILEGES` beside it, every table added after birth is
    invisible to `ava_runner` forever, and nothing re-runs the grant on a
    schedule. It went unnoticed until `20260820T175737_extension-registry.sql`,
    the first post-baseline migration to CREATE a table rather than add columns
    — which came out unreadable on every pure agent-runner, where processes dial
    as `ava_runner` rather than the main identity.
    """
    ensure_runner_role(_IDENTITY, base_admin_url=_admin_url(runner_db), runner_password=_RUNNER_PW)

    # ... and then a migration creates a table, as the cluster's main identity.
    with psycopg.connect(_identity_url(runner_db), autocommit=True) as conn:
        conn.execute("CREATE TABLE post_provision_table (id int PRIMARY KEY)")
        conn.execute("CREATE TABLE post_provision_serial (id bigserial PRIMARY KEY, v int)")

    with psycopg.connect(_runner_url(runner_db), autocommit=True) as conn:
        assert conn.execute("SELECT count(*) FROM post_provision_table").fetchone() == (0,)
        # The sequence half of the same policy: a BIGSERIAL table added later
        # must also carry USAGE on its owning sequence, or the first runner
        # INSERT into it fails on the sequence rather than the table.
        assert conn.execute("SELECT last_value FROM post_provision_serial_id_seq").fetchone() == (
            1,
        )
