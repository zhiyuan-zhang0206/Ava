"""Runner role cases: runner grant matrix."""

from __future__ import annotations

from uuid import uuid4

import psycopg
import pytest
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg import sql
from psycopg.types.json import Jsonb

from base.agents.incarnation.native_restart_models import (
    NativeRestartAcceptance,
    NativeRestartRequest,
)
from base.agents.incarnation.native_work_models import NativeWorkTarget
from tests.base.test_runner_role import (
    _assert_alert_writes_denied,
    _exercise_impersonation_entry_grants,
    _exercise_pause_grants,
    _exercise_understanding_node_grants,
    _exercise_understanding_queue_grants,
    _grant_runner,
    _identity_url,
    _runner_url,
)
from tests.base.test_runner_role import (
    runner_db as runner_db,
)


def test_runner_grant_matrix(runner_db: str) -> None:  # noqa: PLR0915 -- one grant-matrix litany; each line is one exercised surface
    """The design's grant matrix, exercised as ava_runner over the wire."""
    _grant_runner(runner_db)

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
        # agents is gateway-written only: labels go through the gateway API
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("UPDATE agents SET label = 'me' WHERE id = %s", (agent_id,))
        # SDK write surfaces the runner process uses directly:
        # ava.tasks (INSERT + UPDATE) ...
        conn.execute(
            "INSERT INTO agent_tasks (title, description, created_by) VALUES ('t1', 'd', '123')"
        )
        conn.execute("UPDATE agent_tasks SET status = 'done' WHERE title = 't1'")
        # DELETE stays table-scoped: the understanding-node reconciliation
        # added one DELETE surface, not a blanket delete on writable tables.
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("DELETE FROM agent_tasks WHERE title = 't1'")
        # ... and the show-page close at exit (agent_pages UPDATE; the row
        # itself was seeded by the gateway side above)
        conn.execute("UPDATE agent_pages SET closed_at = now() WHERE agent_id = %s", (agent_id,))
        # ava.self.pause_heartbeat: the pause trail (SELECT the previous
        # window + INSERT the new row — regression for task #1932, where the
        # table shipped without a runner grant and every pause_heartbeat
        # INSERT failed with InsufficientPrivilege)
        _exercise_pause_grants(conn, agent_id)
        # the impersonation session trail (INSERT through the lifecycle
        # trigger and the handoff writer, SELECT for readers — regression for
        # task #3549, where the table shipped without a runner grant and lease
        # creation failed with InsufficientPrivilege on
        # agent_impersonation_entries)
        _exercise_impersonation_entry_grants(conn, agent_id)
        # the understanding-layer build (INSERT + UPDATE + SELECT; the manual
        # first-run / ad-hoc regeneration path runs from the runner side —
        # task #3704)
        _exercise_understanding_node_grants(conn, agent_id)
        # the chunk-triggered understanding queue (INSERT + UPDATE + SELECT)
        _exercise_understanding_queue_grants(conn, agent_id)
        # alerts is gateway-written only: the runner reads it but cannot write it
        _assert_alert_writes_denied(conn)
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
            "INSERT INTO host_deploy_state (machine, posture, updated_at)"
            " VALUES ('m1', 'idle', now())"
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


def test_runner_reads_but_cannot_write_deployment_state(runner_db: str) -> None:
    """The version gate row is readable by runners and writable only by the gateway."""
    _grant_runner(runner_db)

    with psycopg.connect(_runner_url(runner_db), autocommit=True) as conn:
        row = conn.execute("SELECT min_code_version FROM deployment_state WHERE id = 1").fetchone()
        assert row is not None
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("UPDATE deployment_state SET min_code_version = 0 WHERE id = 1")


def test_read_grant_reaches_a_table_created_after_provisioning(runner_db: str) -> None:
    """A table a LATER migration creates is still readable by the runner role.

    `test_runner_grant_matrix` cannot reach this: its fixture applies all of
    `schema.sql` before provisioning, so every table existed at grant time.
    The live order is the reverse — install grants once, then migrations keep
    creating tables, and `GRANT SELECT ON ALL TABLES` is a point-in-time loop:
    without the `ALTER DEFAULT PRIVILEGES` beside it every later table stayed
    invisible to `ava_runner` (found on `20260820T175737`, the first
    post-baseline migration to CREATE a table).
    """
    _grant_runner(runner_db)

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


def test_pause_log_write_grant_reaches_a_cluster_born_before_the_table(
    runner_db: str,
) -> None:
    """Task #1932 regression: a cluster born BEFORE heartbeat_pause_log existed.

    Fresh-birth coverage lives in `test_runner_grant_matrix`. The prod shape
    is the reverse: the cluster was born, THEN the migration created the
    table — and the runner's write grant for it is a per-table entry in
    the runner group's matrix, so nothing covered the new table until the
    start-path refresh re-ran the grant layer, and the fleet-wide
    `pause_heartbeat` INSERT failed with InsufficientPrivilege.
    """
    _grant_runner(runner_db)

    # Simulate a cluster born BEFORE the pause table existed: the fixture's
    # fresh schema already carries it, so drop it first; the "migration" then
    # creates it AS the main identity (the role the migration applier dials —
    # the default privileges key on it).
    with psycopg.connect(runner_db, autocommit=True) as conn:
        conn.execute("DROP TABLE heartbeat_pause_log")
    with psycopg.connect(_identity_url(runner_db), autocommit=True) as conn:
        agent_row = conn.execute(
            "INSERT INTO agents (label) VALUES ('seed') RETURNING id"
        ).fetchone()
        assert agent_row is not None
        agent_id: int = agent_row[0]
        conn.execute(
            "CREATE TABLE heartbeat_pause_log ("
            "  id BIGSERIAL PRIMARY KEY,"
            "  agent_id BIGINT NOT NULL REFERENCES agents(id),"
            "  duration_s DOUBLE PRECISION NOT NULL,"
            "  created_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )

    # Before the refresh: SELECT works (default privileges / point-in-time
    # loop), INSERT is denied — exactly the prod symptom.
    with psycopg.connect(_runner_url(runner_db), autocommit=True) as conn:
        conn.execute("SELECT count(*) FROM heartbeat_pause_log")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(
                "INSERT INTO heartbeat_pause_log (agent_id, duration_s) VALUES (%s, 1800)",
                (agent_id,),
            )

    # The start-path refresh (ensure_groups) re-runs the grant layer with the
    # table now present.
    _grant_runner(runner_db)
    with psycopg.connect(_runner_url(runner_db), autocommit=True) as conn:
        conn.execute(
            "INSERT INTO heartbeat_pause_log (agent_id, duration_s) VALUES (%s, 1800)",
            (agent_id,),
        )
        row = conn.execute("SELECT count(*) FROM heartbeat_pause_log").fetchone()
    assert row == (1,)


def test_impersonation_entry_grant_reaches_a_cluster_born_before_the_table(
    runner_db: str,
) -> None:
    """Task #3549 regression: a cluster that adopted the impersonation trail
    (20260913T180056) before the runner grant for its table existed.

    Fresh-birth coverage lives in `test_runner_grant_matrix`; the prod shape
    is the reverse: the cluster was born, THEN the migration created the
    table, so the role could read the trail but creating a lease failed in
    the lifecycle trigger with InsufficientPrivilege on
    agent_impersonation_entries until the start-path refresh re-ran the grant
    layer.
    """
    _grant_runner(runner_db)

    # Simulate a cluster born BEFORE the trail table existed: drop it, then
    # re-create it AS the main identity (the role the migration applier dials
    # — the default privileges key on it).
    with psycopg.connect(runner_db, autocommit=True) as conn:
        conn.execute("DROP TABLE agent_impersonation_entries")
    with psycopg.connect(_identity_url(runner_db), autocommit=True) as conn:
        agent_row = conn.execute(
            "INSERT INTO agents (label) VALUES ('seed') RETURNING id"
        ).fetchone()
        assert agent_row is not None
        agent_id: int = agent_row[0]
        conn.execute(
            "INSERT INTO agents_meta (id, spawner, status) VALUES (%s, 'user', 'idling')",
            (agent_id,),
        )
        conn.execute(
            "CREATE TABLE agent_impersonation_entries ("
            "  lease_id UUID NOT NULL REFERENCES agent_impersonations(id) ON DELETE RESTRICT,"
            "  seq BIGINT NOT NULL CHECK (seq >= 0),"
            "  kind TEXT NOT NULL CHECK (kind IN ('message','lifecycle','sdk_call','api_event')),"
            "  event_key TEXT,"
            "  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),"
            "  payload JSONB NOT NULL,"
            "  PRIMARY KEY(lease_id,seq),"
            "  UNIQUE(lease_id,event_key))"
        )

    def create_lease(conn: psycopg.Connection) -> tuple[object, ...] | None:
        return conn.execute(
            "INSERT INTO agent_impersonations (id, agent_id, source, machine,"
            " status, ttl_seconds, expires_at)"
            " VALUES (gen_random_uuid(), %s, 'codex', 'test-machine', 'requested',"
            " 3600, now() + interval '1 hour') RETURNING id",
            (agent_id,),
        ).fetchone()

    # Before the refresh: reading works (default privileges), creating a lease
    # is denied exactly as prod reported it — the failure surfaces from the
    # lifecycle trigger's INSERT into the trail.
    with psycopg.connect(_runner_url(runner_db), autocommit=True) as conn:
        assert conn.execute("SELECT count(*) FROM agent_impersonation_entries").fetchone() == (0,)
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            create_lease(conn)

    # The start-path refresh re-runs the grant layer with the table present.
    _grant_runner(runner_db)
    with psycopg.connect(_runner_url(runner_db), autocommit=True) as conn:
        lease = create_lease(conn)
        assert lease is not None
        row = conn.execute(
            "SELECT count(*) FROM agent_impersonation_entries WHERE lease_id = %s", (lease[0],)
        ).fetchone()
    assert row == (1,)


def test_refresh_revokes_the_alert_writes_an_earlier_release_granted(runner_db: str) -> None:
    """A cluster born when the runner matrix granted INSERT/UPDATE on alerts
    converges to read-only: the grant refresh revokes what the matrix no longer
    names."""
    _grant_runner(runner_db)
    with psycopg.connect(runner_db, autocommit=True) as conn:
        conn.execute("GRANT INSERT, UPDATE ON alerts TO ava_runner")

    _grant_runner(runner_db)
    with psycopg.connect(_runner_url(runner_db), autocommit=True) as conn:
        _assert_alert_writes_denied(conn)


def test_understanding_queue_insert_grant_reaches_a_cluster_born_before_the_entry(
    runner_db: str,
) -> None:
    """A cluster whose runner surface predates the understanding queue's INSERT entry
    would fail every enqueue with InsufficientPrivilege until the start-path refresh
    re-runs the grant layer."""
    _grant_runner(runner_db)
    with psycopg.connect(runner_db, autocommit=True) as conn:
        conn.execute("REVOKE INSERT ON understanding_chunk_jobs FROM ava_runner")

    with (
        psycopg.connect(_runner_url(runner_db), autocommit=True) as conn,
        pytest.raises(psycopg.errors.InsufficientPrivilege),
    ):
        _exercise_understanding_queue_grants(conn, 880_040)

    _grant_runner(runner_db)
    with psycopg.connect(_runner_url(runner_db), autocommit=True) as conn:
        _exercise_understanding_queue_grants(conn, 880_040)


@pytest.mark.parametrize("refresh", [False, True])
def test_runner_accepts_and_projects_original_restart_receipt_without_delete_grants(
    runner_db: str, refresh: bool
) -> None:
    """Actual source UPDATE projects APPLIED/OBSERVED using the runner login."""
    _grant_runner(runner_db)
    with psycopg.connect(runner_db, autocommit=True) as conn:
        row = conn.execute("INSERT INTO agents DEFAULT VALUES RETURNING id").fetchone()
        assert row is not None
        agent_id = row[0]
        conn.execute("INSERT INTO agents_meta(id,status) VALUES(%s,'idling')", (agent_id,))
        target = NativeWorkTarget(
            protocol=1,
            work_id=uuid4(),
            agent_id=agent_id,
            machine="runner-test",
            generation=uuid4(),
            owner=uuid4(),
        )
        row = conn.execute(
            "INSERT INTO inbound_messages(agent_id,content,kind,source,status,claimed_at,target_generation,target_owner) "
            "VALUES(%s,'','restart','user','claimed',now(),%s,%s) RETURNING id",
            (agent_id, target.generation, target.owner),
        ).fetchone()
        assert row is not None
        command_id = row[0]
        acceptance = NativeRestartAcceptance(
            command_id=command_id, target=target, config_overlay=None
        )
    with psycopg.connect(_runner_url(runner_db), autocommit=True) as conn:
        conn.execute(
            "INSERT INTO native_restart_commands(operation_key,command_id,agent_id,work_id,"
            "target_generation,target_owner,request_hash,request,acceptance) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                "runner-restart",
                command_id,
                agent_id,
                target.work_id,
                target.generation,
                target.owner,
                "0" * 64,
                Jsonb(NativeRestartRequest(target=target).model_dump(mode="json")),
                Jsonb(acceptance.model_dump(mode="json")),
            ),
        )
    if refresh:
        with psycopg.connect(runner_db, autocommit=True) as conn:
            conn.execute("REVOKE UPDATE ON native_restart_commands FROM ava_runner")
    if refresh:
        with (
            psycopg.connect(_runner_url(runner_db), autocommit=True) as conn,
            pytest.raises(psycopg.errors.InsufficientPrivilege),
        ):
            conn.execute("UPDATE inbound_messages SET applied_at=now() WHERE id=%s", (command_id,))
        _grant_runner(runner_db)
    with psycopg.connect(_runner_url(runner_db), autocommit=True) as conn:
        conn.execute("UPDATE inbound_messages SET applied_at=now() WHERE id=%s", (command_id,))
        assert conn.execute(
            "SELECT outcome,applied_at IS NOT NULL,observed_at IS NULL FROM native_restart_commands WHERE command_id=%s",
            (command_id,),
        ).fetchone() == ("applied", True, True)
        conn.execute(
            "UPDATE inbound_messages SET observed_at=now(),status='done' WHERE id=%s", (command_id,)
        )
        assert conn.execute(
            "SELECT outcome,observed_at IS NOT NULL FROM native_restart_commands WHERE command_id=%s",
            (command_id,),
        ).fetchone() == ("observed", True)
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("DELETE FROM native_restart_commands WHERE command_id=%s", (command_id,))


def test_runner_writes_native_work_copy_and_cancel_ack_with_domain_limits(runner_db: str) -> None:
    """The remote runner unit's login can perform only the audited native DML."""
    _grant_runner(runner_db)
    work_id, cancel_id = uuid4(), uuid4()
    with psycopg.connect(runner_db, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO native_cancel_commands(id,work_id,agent_id,operation_key,request,acceptance) "
            "VALUES(%s,%s,1,'runner-cancel','{}','{}')",
            (cancel_id, work_id),
        )
    with psycopg.connect(_runner_url(runner_db), autocommit=True) as conn:
        conn.execute(
            "INSERT INTO native_graph_work(id,agent_id,machine,generation,owner,protocol,phase) "
            "VALUES(%s,1,'runner-test',%s,%s,1,'preparing')",
            (work_id, uuid4(), uuid4()),
        )
        conn.execute("UPDATE native_graph_work SET phase='active' WHERE id=%s", (work_id,))
        conn.execute(
            "UPDATE native_cancel_commands SET outcome='applied',checkpoint_id='original-checkpoint',"
            "settled_at=now() WHERE id=%s",
            (cancel_id,),
        )
        conn.execute(
            "UPDATE native_graph_work SET phase='settled',ended_at=now(),settled_checkpoint_id='original-checkpoint' "
            "WHERE id=%s",
            (work_id,),
        )
        conn.execute(
            "INSERT INTO upload_delivery_copies(batch_id,unit_home,agent_id,request,manifest,storage_machine,storage_directory) "
            "VALUES('fixed-batch','/unit',1,'{}','{}','runner-test','/Downloads/AvaAgent-1')"
        )
        conn.execute(
            "UPDATE upload_delivery_copies SET ready_at=now() WHERE batch_id='fixed-batch'"
        )
        assert conn.execute(
            "SELECT phase,ended_at IS NOT NULL FROM native_graph_work WHERE id=%s", (work_id,)
        ).fetchone() == ("settled", True)
        assert conn.execute(
            "SELECT outcome,checkpoint_id FROM native_cancel_commands WHERE id=%s", (cancel_id,)
        ).fetchone() == ("applied", "original-checkpoint")
        assert conn.execute(
            "SELECT ready_at IS NOT NULL FROM upload_delivery_copies WHERE batch_id='fixed-batch'"
        ).fetchone() == (True,)
        for table in ("native_graph_work", "native_cancel_commands", "upload_delivery_copies"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(sql.SQL("DELETE FROM {}").format(sql.Identifier(table)))
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("INSERT INTO native_cancel_commands(id) VALUES(gen_random_uuid())")
        for table in ("upload_delivery_batches", "agent_creation_snapshots"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(sql.SQL("INSERT INTO {} DEFAULT VALUES").format(sql.Identifier(table)))
