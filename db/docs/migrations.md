# Database migrations

The model itself — baseline vs post-baseline deltas, the applied **set** keyed by
migration name, the immutability of merged migrations, expand-contract for lossy
operations, and the bidirectional `assert_schema_current` check — is in
[migration model](../../base/deploy/schema/docs/migrations.ava.okf.md). What follows is how you operate it.

## Applying migrations

There is no standalone migrate command. Pending migrations are applied as a step of
`ava start` (early in boot, after Postgres is up and before the schema-current assertion), so
any restart that crosses a schema change catches the DB up automatically.

Production upgrades run `python -m cli.fleet_update down` and `up` (attended, with
user approval; see the runbook's "Updating a networked cluster in source mode"):
the gateway's cold `ava start` in the `up` half applies the pending migrations.
For a manual catch-up, run `ava start` directly, which applies pending
migrations on the way up.

## Adding a new migration

1. Write `migrations/YYYYMMDDTHHMMSS_<kebab-name>.sql` — the prefix is a
   second-precision UTC timestamp (`date -u +%Y%m%dT%H%M%S`), pure SQL, don't INSERT
   schema_migrations (the runner does it)
2. There is no down migration. A mistake in a merged migration is fixed forward by a new
   migration; the merged file is never edited, deleted or renamed (lint check 4). Lossy
   operations go expand-contract: ship the code that stops using the object first, drop it in
   a later migration
3. Sync the corresponding schema change into `db/schema.sql` (the baseline stays current).
   When the change is **non-idempotent** (strict — no `IF NOT EXISTS` / `OR REPLACE`),
   also stamp this migration's name in the seed section — `INSERT INTO schema_migrations
   (name) VALUES ('<name>')` — because a fresh DB replays every unseeded migration and
   would fail on `already exists` (lint check 7 enforces the mechanically detectable cases)
4. PR review focus: running `db/schema.sql` on a fresh DB and running the baseline + all
   post-baseline migrations on a dev DB must converge to the same schema

## Pre-commit lint

In CI, `scripts/content_lint/lint_migrations.py` statically checks the timestamp filename format
(`YYYYMMDDTHHMMSS_<kebab-name>.sql`, a real datetime), name uniqueness, that no migration already on main (against the merge-base with `origin/main`,
or `--base` in CI) is modified, deleted or renamed, that
`db/schema.sql` stamps the baseline sentinel and no longer carries a `generate_series` seed,
and that a migration whose strict (non-idempotent) DDL is already folded into the baseline
is stamped in the seed — an unstamped strict delta dies on the first fresh-DB bootstrap, so
lint fails it early. It also refuses `SET ROLE` / `RESET ROLE` / session-authorization
changes: migrations run as the OS-user administrator acting as the schema owner, so every
object stays owner-owned and only the owner's privileges apply. Local pre-check: `.venv/bin/python scripts/content_lint/lint_migrations.py`. There is no
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
