---
name: add-a-migration
description: Use when adding, changing, or rolling back a database migration — the timestamp filename, the mandatory paired .down.sql, keeping db/schema.sql current, and the trigger-body smoke test.
---

# Add a migration

The model itself — baseline vs post-baseline deltas, the applied **set** keyed by
migration name, the mandatory `.down.sql` pairing, expand-contract for lossy
operations, and the bidirectional `assert_schema_current` check — is in
`shared/migrations.ava.okf.md`. What follows is how you operate it.

## Applying migrations

There is no standalone migrate command. Pending migrations are applied as a step of
`ava start` (early in boot, after Postgres is up and before the schema-current assertion), so
any restart that crosses a schema change catches the DB up automatically.

Real production upgrades go through `ava cluster update` (the CLI — the only
update entry point since `ava.self.update()` was removed 2026-08; run by the
Release agent with user approval), which on the gateway ends in a fresh
`ava start` that migrates. For a manual catch-up, run `ava cluster update` (or just `ava start`, which
applies pending migrations on the way up).

## Adding a new migration

1. Write `migrations/YYYYMMDDTHHMMSS_<kebab-name>.sql` — the prefix is a
   second-precision UTC timestamp (`date -u +%Y%m%dT%H%M%S`), pure SQL, don't INSERT
   schema_migrations (the runner does it)
2. Write the paired `migrations/YYYYMMDDTHHMMSS_<kebab-name>.down.sql` reversing it
   (mandatory — the baseline is the rollback floor, so everything above it must be reversible)
3. Sync the corresponding schema change into `db/schema.sql` (the baseline stays current)
4. PR review focus: running `db/schema.sql` on a fresh DB and running the baseline + all
   post-baseline migrations on a dev DB must converge to the same schema

## Pre-commit lint

In CI, `scripts/lint_migrations.py` statically checks the timestamp filename format
(`YYYYMMDDTHHMMSS_<kebab-name>.sql`, a real datetime), name uniqueness, up/down pairing, and
that `db/schema.sql` stamps the baseline sentinel and no longer carries a `generate_series`
seed. Local pre-check: `.venv/bin/python scripts/lint_migrations.py`. There is no
continuity / next-number / cross-branch-collision check — timestamp names are collision-free by
construction.

## Baseline-schema smoke test

`scripts/test_migrations_apply.sh` (matching ci.yml step) builds a fresh empty DB, applies the
baseline `db/schema.sql`, INSERTs a fixture agent + page + UPDATEs
`agents_meta SET status='terminated'`, and verifies the cascade trigger
actually fires (DO block asserts `agent_pages.closed_at` was
propagated). **Adding any migration that changes a trigger / function
body must add an exercise line to `test_migrations_apply.sh`** —
`CREATE OR REPLACE FUNCTION` at apply time does NOT validate PL/pgSQL
column references; body bugs surface only when the trigger actually
fires (a wrong `NEW.agent_id` reference
in #369's rename slipped past lint + pytest + apply-time validation,
caught only by real cross-machine force-terminate in prod).
