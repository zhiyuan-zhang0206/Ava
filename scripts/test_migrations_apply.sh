#!/usr/bin/env bash
# Apply the baseline db/schema.sql on a fresh empty DB + exercise trigger paths,
# guarding against schema bodies silently referencing wrong column / table names
# (CREATE OR REPLACE FUNCTION does not check column reference validity; a
# stand-alone apply will not blow up).
#
# Since the 2026-07-19 re-baseline, db/schema.sql is the squashed baseline (the
# full current schema, including trigger functions); migrations/ holds only
# post-baseline deltas. So the fresh-bootstrap smoke applies schema.sql, not the
# migration sequence — the trigger bodies it exercises now live there.
#
# Bug this shape catches (2026-05-20, then folded into the baseline): a
# cascade_close_agent_pages() written as NEW.agent_id while the trigger fires on
# agents_meta (column is `id`), causing every UPDATE status='terminated' to 5xx.
#
# Usage:
#   AVA_DB_URL=postgresql://ava@host:5432/ scripts/test_migrations_apply.sh
#   (or directly PGHOST=postgres PGUSER=ava scripts/test_migrations_apply.sh)
# Default: connect to postgres:5432 (CI container service sidecar default name).
set -euo pipefail

PGHOST="${PGHOST:-${AVA_PGHOST:-postgres}}"
PGUSER="${PGUSER:-${AVA_PGUSER:-ava}}"
PGPORT="${PGPORT:-5432}"
ADMIN_DB="${PGDATABASE:-ava}"
export PGHOST PGUSER PGPORT

TEST_DB="ava_migration_smoke_$$"
FULL_DB="ava_migration_full_$$"

cleanup() {
    psql -d "$ADMIN_DB" -c "DROP DATABASE IF EXISTS $TEST_DB" >/dev/null 2>&1 || true
    psql -d "$ADMIN_DB" -c "DROP DATABASE IF EXISTS $FULL_DB" >/dev/null 2>&1 || true
}
trap cleanup EXIT

REPO_ROOT="${AVA_REPO_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
cd "$REPO_ROOT"

echo "-> create $TEST_DB on $PGHOST:$PGPORT"
psql -d "$ADMIN_DB" -v ON_ERROR_STOP=1 -c "CREATE DATABASE $TEST_DB"

echo "-> apply baseline db/schema.sql"
psql -d "$TEST_DB" -v ON_ERROR_STOP=1 -q -f db/schema.sql
echo "  ok"

echo "-> retired deploy storage is absent and the version-gate row survives"
psql -d "$TEST_DB" -v ON_ERROR_STOP=1 <<'SQL'
DO $$
BEGIN
    IF to_regclass('public.cluster_pin') IS NOT NULL
       OR to_regclass('public.cluster_last_update') IS NOT NULL
       OR to_regclass('public.agent_watchers') IS NOT NULL
       OR to_regprocedure('public.lock_runtime_publication_admission()') IS NOT NULL THEN
        RAISE EXCEPTION 'retired deploy or watcher storage is back in the baseline';
    END IF;
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_schema = 'public' AND table_name = 'agents_meta'
                 AND column_name = 'closed_at') THEN
        RAISE EXCEPTION 'agents_meta.closed_at is back in the baseline';
    END IF;
    IF (SELECT array_agg(column_name::text ORDER BY column_name)
        FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'deployment_state')
       IS DISTINCT FROM ARRAY['id', 'min_code_version'] THEN
        RAISE EXCEPTION 'deployment_state must hold only id and min_code_version';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM deployment_state WHERE id = 1) THEN
        RAISE EXCEPTION 'the deployment_state singleton row is missing (version gate)';
    END IF;
END $$;
SQL
echo "  ok"

echo "-> audit_events is append-only: INSERT lands, UPDATE / DELETE / TRUNCATE are rejected"
psql -d "$TEST_DB" -v ON_ERROR_STOP=1 <<'SQL'
INSERT INTO audit_events (event_uid, ts, machine, process, event_name, level, source)
    VALUES (1, now(), 'smoke-machine', 'smoke', 'spawn', 'info', 'user');
DO $$
DECLARE
    statement TEXT;
BEGIN
    FOREACH statement IN ARRAY ARRAY[
        'UPDATE audit_events SET level = ''error''',
        'DELETE FROM audit_events',
        'TRUNCATE audit_events'
    ] LOOP
        BEGIN
            EXECUTE statement;
            RAISE EXCEPTION 'audit_events accepted: %', statement;
        EXCEPTION WHEN raise_exception THEN
            IF SQLERRM NOT LIKE 'audit_events is append-only%' THEN
                RAISE;
            END IF;
        END;
    END LOOP;
    IF (SELECT count(*) FROM audit_events) <> 1 THEN
        RAISE EXCEPTION 'audit_events lost its row';
    END IF;
END $$;
SQL
echo "  ok"

echo "-> trigger smoke: exercise agents_meta termination triggers"
psql -d "$TEST_DB" -v ON_ERROR_STOP=1 <<'SQL'
INSERT INTO agents (label) VALUES ('smoke-agent');
INSERT INTO agents_meta (id, status, machine)
    VALUES ((SELECT max(id) FROM agents), 'running', 'smoke-machine');
INSERT INTO agent_pages (agent_id, name, port)
    VALUES ((SELECT max(id) FROM agents), 'show', 9001);
INSERT INTO agent_pages (agent_id, name, port, serve_dir)
    VALUES ((SELECT max(id) FROM agents), 'serve', 9002, '/tmp/serve');
UPDATE agents_meta SET status = 'terminated'
    WHERE id = (SELECT max(id) FROM agents);

-- Observed lifecycle time follows real row transitions, even without telemetry.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM agent_lifecycle_intervals
        WHERE agent_id = (SELECT max(id) FROM agents) AND ended_at >= started_at
    ) THEN
        RAISE EXCEPTION 'termination did not close the lifecycle interval';
    END IF;
END $$;
DO $$
DECLARE show_closed_count INT;
DECLARE serve_open_count INT;
BEGIN
    SELECT COUNT(*) FILTER (WHERE name = 'show' AND closed_at IS NOT NULL),
           COUNT(*) FILTER (WHERE name = 'serve' AND closed_at IS NULL)
      INTO show_closed_count, serve_open_count
        FROM agent_pages
        WHERE name IN ('show', 'serve');
    IF show_closed_count <> 1 OR serve_open_count <> 1 THEN
        RAISE EXCEPTION 'cascade_close_agent_pages wrong result — show_closed_count=%, serve_open_count=%',
            show_closed_count, serve_open_count;
    END IF;
END $$;

-- Check resurrection only after termination's page closure; it reopens pages.
UPDATE agents_meta SET status = 'idling'
    WHERE id = (SELECT max(id) FROM agents);
UPDATE agents_meta SET status = 'running'
    WHERE id = (SELECT max(id) FROM agents);
DO $$
BEGIN
    IF (SELECT count(*) FROM agent_lifecycle_intervals
        WHERE agent_id = (SELECT max(id) FROM agents)) <> 2
       OR (SELECT count(*) FROM agent_lifecycle_intervals
           WHERE agent_id = (SELECT max(id) FROM agents) AND ended_at IS NULL) <> 1 THEN
        RAISE EXCEPTION 'resurrection or nonterminal transition corrupted lifecycle intervals';
    END IF;
END $$;

-- Exercise lease closure on the fresh baseline.
INSERT INTO agents (id, label) VALUES (991005, 'impersonation-owner-smoke');
INSERT INTO agents_meta (id, status, machine, runtime_generation, runtime_owner)
    VALUES (991005, 'idling', 'smoke-machine',
            '00000000-0000-0000-0000-000000000003',
            '00000000-0000-0000-0000-000000000004');
INSERT INTO agents (id, label) VALUES (991006, 'manifest-owner-smoke');
INSERT INTO agents_meta (id, status, machine, runtime_generation, runtime_owner)
    VALUES (991006, 'idling', 'smoke-machine',
            '00000000-0000-0000-0000-000000000003',
            '00000000-0000-0000-0000-000000000004');
INSERT INTO agent_impersonations (
    id, agent_id, source, machine, token_hash, status, ttl_seconds, expires_at,
    accepted_generation, accepted_owner, automatic
) VALUES (
    '00000000-0000-0000-0000-000000000005', 991005, 'external_agent:smoke',
    'smoke-machine', 'smoke-token', 'active', 300, clock_timestamp() + interval '5 minutes',
    '00000000-0000-0000-0000-000000000001', '00000000-0000-0000-0000-000000000002', TRUE
);
-- Exercise allocation, both history writers, and the permanent-history guard.
INSERT INTO inbound_messages(agent_id,kind,source,content)
VALUES(991005,'chat','user','Permanent smoke message');
UPDATE agent_impersonations SET expires_at=expires_at+interval '1 minute' WHERE agent_id=991005;
DO $$
DECLARE protected BOOLEAN := FALSE;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM agent_impersonations WHERE agent_id=991005 AND session_id=0 AND name='Session 0') THEN
        RAISE EXCEPTION 'Agent-scoped session allocation failed';
    END IF;
    IF (SELECT count(*) FROM agent_impersonation_entries) <> 3 THEN
        RAISE EXCEPTION 'Lifecycle or inbound history trigger did not run';
    END IF;
    BEGIN
        DELETE FROM agent_impersonation_entries;
    EXCEPTION WHEN raise_exception THEN protected := TRUE;
    END;
    IF NOT protected THEN RAISE EXCEPTION 'Permanent history allowed deletion'; END IF;
END $$;
UPDATE agent_impersonations SET status = 'released' WHERE agent_id = 991005;
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM agents_meta WHERE id = 991005
          AND runtime_generation = '00000000-0000-0000-0000-000000000001'
          AND runtime_owner = '00000000-0000-0000-0000-000000000002'
    ) THEN
        RAISE EXCEPTION 'lease closure did not restore the native incarnation';
    END IF;
END $$;

-- Exercise the surviving closure primitives on the fresh squashed baseline:
-- a protocol-v1 lease still closes admission and takes a participant lock, but
-- no path completes it any more (the certification path was contracted away).
INSERT INTO agent_impersonations (
    id, agent_id, source, machine, token_hash, status, ttl_seconds, expires_at,
    accepted_generation, accepted_owner, automatic, event_delivery_protocol_version, activated_at
) VALUES (
    '00000000-0000-0000-0000-000000000006', 991006, 'external_agent:closure-smoke',
    'smoke-machine', 'closure-smoke-token', 'accepted', 300, clock_timestamp() + interval '5 minutes',
    '00000000-0000-0000-0000-000000000001', '00000000-0000-0000-0000-000000000002', TRUE, 1,
    NULL
);
SELECT close_impersonation_event_manifest_admission('00000000-0000-0000-0000-000000000006');
DO $$
BEGIN
    IF lock_impersonation_event_participant(
        '00000000-0000-0000-0000-000000000006', 'missing-smoke-receipt'
    ) IS NOT NULL THEN
        RAISE EXCEPTION 'missing receipt unexpectedly acquired a lock';
    END IF;
END $$;
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM agent_impersonations
               WHERE id='00000000-0000-0000-0000-000000000006'
                 AND events_completed_at IS NOT NULL) THEN
        RAISE EXCEPTION 'a protocol-v1 lease must never complete without the certification path';
    END IF;
    BEGIN
        UPDATE agent_impersonations SET event_delivery_protocol_version=NULL
        WHERE id='00000000-0000-0000-0000-000000000006';
    EXCEPTION WHEN raise_exception THEN
        RETURN;
    END;
    RAISE EXCEPTION 'protocol version was mutable after admission';
END $$;

-- Agent termination ends an open protocol-v1 lease and closes its manifest
-- admission through the SECURITY DEFINER door, like every other lease end.
INSERT INTO agents (id, label) VALUES (991007, 'terminated-manifest-owner-smoke');
INSERT INTO agents_meta (id, status, machine, runtime_generation, runtime_owner)
    VALUES (991007, 'idling', 'smoke-machine',
            '00000000-0000-0000-0000-000000000003',
            '00000000-0000-0000-0000-000000000004');
INSERT INTO agent_impersonations (
    id, agent_id, source, machine, token_hash, status, ttl_seconds, expires_at,
    accepted_generation, accepted_owner, automatic, event_delivery_protocol_version, activated_at
) VALUES (
    '00000000-0000-0000-0000-000000000007', 991007, 'external_agent:terminate-smoke',
    'smoke-machine', 'terminate-smoke-token', 'active', 300, clock_timestamp() + interval '5 minutes',
    '00000000-0000-0000-0000-000000000001', '00000000-0000-0000-0000-000000000002', TRUE, 1,
    clock_timestamp()
);
UPDATE agents_meta SET status = 'terminated' WHERE id = 991007;
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM agent_impersonations
                   WHERE id='00000000-0000-0000-0000-000000000007'
                     AND status='expired' AND ended_at IS NOT NULL
                     AND manifest_admission_closed_at IS NOT NULL) THEN
        RAISE EXCEPTION 'agent termination did not close the lease manifest admission';
    END IF;
    IF (SELECT rejection_reason FROM agent_impersonations
        WHERE id='00000000-0000-0000-0000-000000000007')
        IS DISTINCT FROM 'terminated: agent was terminated'
       OR (SELECT array_agg(payload->>'note_tag' ORDER BY created_at,id)
           FROM inbound_messages WHERE agent_id=991007 AND kind='system_note')
           IS DISTINCT FROM ARRAY['impersonation','lifecycle_terminate']::text[] THEN
        RAISE EXCEPTION 'termination interruption notices missing or out of order';
    END IF;
END $$;

-- Log-native lease (protocol v2): sources append event rows while open, the guard
-- refuses rows for a closed source, and the lease completes by predicate when its
-- end finds every source sealed (agent termination ends it in SQL).
INSERT INTO agents (id, label) VALUES (991008, 'log-native-owner-smoke');
INSERT INTO agents_meta (id, status, machine, runtime_generation, runtime_owner)
    VALUES (991008, 'idling', 'smoke-machine',
            '00000000-0000-0000-0000-000000000003',
            '00000000-0000-0000-0000-000000000004');
INSERT INTO agent_impersonations (
    id, agent_id, source, machine, token_hash, status, ttl_seconds, expires_at,
    accepted_generation, accepted_owner, automatic, event_delivery_protocol_version, activated_at
) VALUES (
    '00000000-0000-0000-0000-000000000008', 991008, 'external_agent:log-native-smoke',
    'smoke-machine', 'log-native-smoke-token', 'active', 300, clock_timestamp() + interval '5 minutes',
    '00000000-0000-0000-0000-000000000001', '00000000-0000-0000-0000-000000000002', TRUE, 2,
    clock_timestamp()
);
INSERT INTO agent_impersonation_event_participants(lease_id, source_key, state)
VALUES ('00000000-0000-0000-0000-000000000008', 'smoke-source', 'open');
UPDATE agent_impersonations SET next_entry = next_entry + 1
WHERE id='00000000-0000-0000-0000-000000000008';
INSERT INTO agent_impersonation_entries(lease_id, seq, kind, event_key, payload, source_key)
SELECT id, next_entry - 1, 'sdk_call', 'event:smoke-1', '{}'::jsonb, 'smoke-source'
FROM agent_impersonations WHERE id='00000000-0000-0000-0000-000000000008';
SELECT seal_impersonation_event_participant(
    '00000000-0000-0000-0000-000000000008', 'smoke-source', 'sealed', NULL, 1, NULL
);
DO $$
DECLARE refused BOOLEAN := FALSE;
BEGIN
    IF EXISTS (SELECT 1 FROM agent_impersonations
               WHERE id='00000000-0000-0000-0000-000000000008'
                 AND events_completed_at IS NOT NULL) THEN
        RAISE EXCEPTION 'log-native lease completed before it ended';
    END IF;
    BEGIN
        INSERT INTO agent_impersonation_entries(lease_id, seq, kind, event_key, payload, source_key)
        VALUES ('00000000-0000-0000-0000-000000000008', 9999, 'sdk_call', 'event:smoke-2',
                '{}'::jsonb, 'smoke-source');
    EXCEPTION WHEN raise_exception THEN refused := TRUE;
    END;
    IF NOT refused THEN RAISE EXCEPTION 'a sealed source accepted another event row'; END IF;
END $$;
UPDATE agents_meta SET status = 'terminated' WHERE id = 991008;
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM agent_impersonations
                   WHERE id='00000000-0000-0000-0000-000000000008'
                     AND status='expired' AND events_completed_at IS NOT NULL) THEN
        RAISE EXCEPTION 'ending a fully sealed log-native lease did not complete its event log';
    END IF;
END $$;

-- Lifecycle status transitions preserve spawn lineage, even when the parent is
-- terminated. This protects against a trigger reintroducing a spawner rewrite.
INSERT INTO agents (id, label) VALUES
    (991001, 'spawner-smoke-grandparent'),
    (991002, 'spawner-smoke-parent'),
    (991003, 'spawner-smoke-child'),
    (991004, 'spawner-smoke-terminated-child');
INSERT INTO agents_meta (id, spawner, status) VALUES
    (991001, 'user', 'running'),
    (991002, 'agent:991001', 'running'),
    (991003, 'agent:991002', 'running'),
    (991004, 'agent:991002', 'terminated');
UPDATE agents_meta SET status = 'terminated' WHERE id = 991002;
UPDATE agents_meta SET status = 'idling' WHERE id = 991004;

DO $$
DECLARE child_spawner TEXT;
DECLARE resurrected_spawner TEXT;
BEGIN
    SELECT spawner INTO child_spawner FROM agents_meta WHERE id = 991003;
    IF child_spawner <> 'agent:991002' THEN
        RAISE EXCEPTION 'terminating parent rewrote child spawner — child_spawner=%',
            child_spawner;
    END IF;
    SELECT spawner INTO resurrected_spawner FROM agents_meta WHERE id = 991004;
    IF resurrected_spawner <> 'agent:991002' THEN
        RAISE EXCEPTION 'resurrecting agent rewrote spawner — resurrected_spawner=%',
            resurrected_spawner;
    END IF;
END $$;
SQL

echo "-> lifecycle pointer->done guard smoke: exercise the commit-time fence"
psql -d "$TEST_DB" -v ON_ERROR_STOP=1 <<'SQL'
INSERT INTO agents (id, label) VALUES (992001, 'pointer-guard-smoke');
INSERT INTO agents_meta (id, status) VALUES (992001, 'terminated');
-- Same-transaction settle passes: the documented legitimate shape.
INSERT INTO inbound_messages (id, agent_id, content, kind, source, status, applied_at, claimed_at, target_generation, target_owner)
    VALUES (9920011, 992001, '', 'terminate', 'user', 'claimed', clock_timestamp(), clock_timestamp(), gen_random_uuid(), gen_random_uuid());
UPDATE agents_meta SET lifecycle_command_id = 9920011 WHERE id = 992001;
BEGIN;
UPDATE inbound_messages SET status = 'done' WHERE id = 9920011;
UPDATE agents_meta SET lifecycle_command_id = NULL WHERE id = 992001;
COMMIT;
SQL
# A torn commit (done with the pointer left alive) must be REJECTED at COMMIT.
if psql -d "$TEST_DB" -v ON_ERROR_STOP=1 <<'SQL'
INSERT INTO inbound_messages (id, agent_id, content, kind, source, status, applied_at, claimed_at, target_generation, target_owner)
    VALUES (9920012, 992001, '', 'terminate', 'user', 'claimed', clock_timestamp(), clock_timestamp(), gen_random_uuid(), gen_random_uuid());
UPDATE agents_meta SET lifecycle_command_id = 9920012 WHERE id = 992001;
BEGIN;
UPDATE inbound_messages SET status = 'done' WHERE id = 9920012;
COMMIT;
SQL
then
    echo "lifecycle pointer->done guard did NOT fire on a torn commit"
    exit 1
fi

echo "-> born_spawner baseline smoke: append-only trigger"
psql -d "$TEST_DB" -v ON_ERROR_STOP=1 <<'SQL'
DO $$
DECLARE rejected_update BOOLEAN := FALSE;
BEGIN
    BEGIN
        UPDATE agents_meta SET born_spawner = 'agent:1' WHERE id = 991001;
    EXCEPTION WHEN raise_exception THEN rejected_update := TRUE;
    END;
    IF NOT rejected_update THEN
        RAISE EXCEPTION 'born_spawner update was not rejected';
    END IF;
END $$;
SQL

echo "-> convergence: db/schema.sql alone vs baseline-pending migrations (pg_dump --schema-only)"
psql -d "$ADMIN_DB" -v ON_ERROR_STOP=1 -c "CREATE DATABASE $FULL_DB"
psql -d "$FULL_DB" -v ON_ERROR_STOP=1 -q -f db/schema.sql
for f in migrations/*.sql; do
    migration_name="${f##*/}"
    migration_name="${migration_name%.sql}"
    # A current baseline can fold a non-idempotent migration and stamp its name
    # in schema_migrations. Match the runtime applier: fresh DBs skip that
    # already-represented delta; existing DBs without the marker execute it.
    if psql -d "$FULL_DB" -v ON_ERROR_STOP=1 -v migration_name="$migration_name" -Atq <<'SQL' | grep -qx 't'
SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name = :'migration_name');
SQL
    then
        continue
    fi
    psql -d "$FULL_DB" -v ON_ERROR_STOP=1 -q -f "$f"
done

BASELINE_DUMP=$(mktemp)
FULL_DUMP=$(mktemp)
pg_dump -d "$TEST_DB" --schema-only -O -x > "$BASELINE_DUMP"
pg_dump -d "$FULL_DB" --schema-only -O -x > "$FULL_DUMP"
# pg_dump 17.10 emits a per-session random \restrict/\unrestrict token — strip
# it (and any other psql meta line) so the comparison is purely schema.
sed -i '' '/^\\restrict /d; /^\\unrestrict /d' "$BASELINE_DUMP" "$FULL_DUMP" 2>/dev/null     || sed -i '/^\\restrict /d; /^\\unrestrict /d' "$BASELINE_DUMP" "$FULL_DUMP"
if ! diff -u "$BASELINE_DUMP" "$FULL_DUMP"; then
    echo "FAIL: db/schema.sql is NOT the squashed net effect of the migrations —"
    echo "the diff above is what the baseline-pending migrations add/change vs"
    echo "the baseline. A new migration must also reflect its change in db/schema.sql"
    echo "and mark its name in the baseline seed when that folded delta is"
    echo "deliberately non-idempotent."
    rm -f "$BASELINE_DUMP" "$FULL_DUMP"
    exit 1
fi
rm -f "$BASELINE_DUMP" "$FULL_DUMP"
echo "ok convergence: schema.sql == baseline + migrations net schema"

echo "ok migrations apply + trigger smoke passed ($TEST_DB)"
