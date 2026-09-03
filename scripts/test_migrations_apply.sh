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
BORN_SPAWNER_DB="ava_born_spawner_smoke_$$"

cleanup() {
    psql -d "$ADMIN_DB" -c "DROP DATABASE IF EXISTS $TEST_DB" >/dev/null 2>&1 || true
    psql -d "$ADMIN_DB" -c "DROP DATABASE IF EXISTS $FULL_DB" >/dev/null 2>&1 || true
    psql -d "$ADMIN_DB" -c "DROP DATABASE IF EXISTS $BORN_SPAWNER_DB" >/dev/null 2>&1 || true
}
trap cleanup EXIT

REPO_ROOT="${AVA_REPO_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
cd "$REPO_ROOT"

echo "-> create $TEST_DB on $PGHOST:$PGPORT"
psql -d "$ADMIN_DB" -v ON_ERROR_STOP=1 -c "CREATE DATABASE $TEST_DB"

echo "-> apply baseline db/schema.sql"
psql -d "$TEST_DB" -v ON_ERROR_STOP=1 -q -f db/schema.sql
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

echo "-> born_spawner migration smoke: backfill and append-only trigger"
psql -d "$ADMIN_DB" -v ON_ERROR_STOP=1 -c "CREATE DATABASE $BORN_SPAWNER_DB"
psql -d "$BORN_SPAWNER_DB" -v ON_ERROR_STOP=1 <<'SQL'
CREATE TABLE agents_meta (
    id BIGINT PRIMARY KEY,
    spawner TEXT NOT NULL,
    fork_source_agent_id BIGINT,
    spawned_at TIMESTAMPTZ NOT NULL
);
CREATE TABLE inbound_messages (
    id BIGINT PRIMARY KEY,
    agent_id BIGINT NOT NULL,
    kind TEXT NOT NULL,
    source TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);
INSERT INTO agents_meta (id, spawner, fork_source_agent_id, spawned_at) VALUES
    (991101, 'agent:405', 2524, '2026-09-03 00:00:00+00'),
    (991102, 'agent:405', NULL, '2026-09-03 00:00:00+00'),
    (991103, 'user', NULL, '2026-09-03 00:00:00+00'),
    (991104, 'user', NULL, '2026-08-10 00:00:00+00');
INSERT INTO inbound_messages (id, agent_id, kind, source, created_at) VALUES
    (1, 991102, 'chat', 'agent:2524', '2026-09-03 00:00:01+00'),
    (2, 991103, 'chat', 'agent:565', '2026-09-03 00:10:01+00'),
    (3, 991104, 'chat', 'agent:565', '2026-08-10 00:01:00+00');
SQL
psql -d "$BORN_SPAWNER_DB" -v ON_ERROR_STOP=1 -f migrations/20260903T175722_add-born-spawner.sql
psql -d "$BORN_SPAWNER_DB" -v ON_ERROR_STOP=1 <<'SQL'
DO $$
DECLARE rejected_update BOOLEAN := FALSE;
BEGIN
    IF (SELECT born_spawner FROM agents_meta WHERE id = 991101) <> 'agent:2524' THEN
        RAISE EXCEPTION 'fork born_spawner backfill failed';
    END IF;
    IF (SELECT born_spawner FROM agents_meta WHERE id = 991102) <> 'agent:405' THEN
        RAISE EXCEPTION 'post-ruling spawner must outrank the timed agent chat';
    END IF;
    IF (SELECT born_spawner FROM agents_meta WHERE id = 991103) <> 'user' THEN
        RAISE EXCEPTION 'late agent chat must leave spawner fallback';
    END IF;
    IF (SELECT born_spawner FROM agents_meta WHERE id = 991104) <> 'agent:565' THEN
        RAISE EXCEPTION 'pre-ruling timed agent chat born_spawner backfill failed';
    END IF;

    BEGIN
        UPDATE agents_meta SET born_spawner = 'agent:1' WHERE id = 991101;
    EXCEPTION WHEN raise_exception THEN
        rejected_update := TRUE;
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
    case "$f" in
        *.down.sql) continue ;;
    esac
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
