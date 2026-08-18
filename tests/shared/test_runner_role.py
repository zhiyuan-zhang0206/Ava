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

import psycopg
import pytest
from langgraph.checkpoint.postgres import PostgresSaver

from shared.cluster import ensure_checkpoint_schema, ensure_runner_role
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
    the .env edit + `ava cluster ensure-runner-role` self-heal path."""
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
        ensure_checkpoint_schema(identity, base_admin_url=admin, cluster_secret=_CLUSTER_SECRET)
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
            "INSERT INTO agents_meta (id, spawner, status) VALUES (%s, 'user', 'allocated')",
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
        # ... and the page close at exit (agent_pages UPDATE; the row itself
        # was seeded by the gateway side above)
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
        # behavior (any DDL must fail under ava_runner); the agent boot therefore
        # skips setup() when the schema is present
        # (agent/_process_boot._checkpoint_schema_present), which loses nothing —
        # setup() is a no-op on an up-to-date schema.
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
