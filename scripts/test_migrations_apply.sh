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

echo "-> trigger smoke: exercise cascade_close_agent_pages on agents_meta UPDATE"
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
SQL

echo "-> convergence: db/schema.sql alone vs schema.sql + all migrations (pg_dump --schema-only)"
psql -d "$ADMIN_DB" -v ON_ERROR_STOP=1 -c "CREATE DATABASE $FULL_DB"
psql -d "$FULL_DB" -v ON_ERROR_STOP=1 -q -f db/schema.sql
for f in migrations/*.sql; do
    case "$f" in
        *.down.sql) continue ;;
    esac
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
    echo "the diff above is what the post-baseline migrations add/change vs the"
    echo "baseline. A new migration must also reflect its change in db/schema.sql"
    echo "(audit P1-1; a fresh baseline replays the deltas, so a lagging baseline"
    echo "only stays green because every migration is idempotent)."
    rm -f "$BASELINE_DUMP" "$FULL_DUMP"
    exit 1
fi
rm -f "$BASELINE_DUMP" "$FULL_DUMP"
echo "ok convergence: schema.sql == baseline + migrations net schema"

echo "ok migrations apply + trigger smoke passed ($TEST_DB)"
