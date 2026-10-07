---
type: doc
title: Schema Migrations (baseline + deltas)
description: '`db/schema.sql` is the squashed baseline; `migrations/YYYY/MM/DD/YYYYMMDDTHHMMSS_<name>.sql` are post-baseline deltas tracked as an applied SET, not a high-water integer. `base/deploy/schema/migrations.py` applies them and asserts version in both directions at every daemon start.'
tags:
- base
- library
- database
---

# Schema Migrations (baseline + deltas)

## Two places, one schema

- **`db/schema.sql`** — the squashed **baseline**: the full current schema a
  fresh DB bootstraps from, and the source of truth for what the schema is now.
  `base.cluster.provision_database` applies it to each cluster's own database
  (created owned by the cluster's role, applied by the administrator acting as
  that role so every object is role-owned); tests build standalone DBs the same
  way. It stamps
  the sentinel `00000000T000000_baseline` and current reset anchor
  `20260923T031516_schema-baseline` into `schema_migrations`.
  **A schema change must be reflected here in the same commit.**
- **`migrations/YYYY/MM/DD/YYYYMMDDTHHMMSS_<kebab-name>.sql`** — post-baseline deltas. There
  are no down migrations; a merged migration name and its SQL bytes are immutable; directory-only moves retain both
  (`lint_migrations.py`, checked against the merge-base with `origin/main`).

## Applied set, not a version number

`base.deploy.schema.migrations.apply_pending_migrations` applies every file whose **name** is
not yet in the DB's applied set, in name (≈ chronological) order. Each file runs
as a single transaction; the **runner INSERTs the file's name into
`schema_migrations`** on completion (migration files must NOT insert
themselves — a self-insert would collide on the primary key) — `name TEXT
PRIMARY KEY`, a **set**, not a high-water integer.

That is the whole point of the timestamp prefix (second-precision UTC,
`date -u +%Y%m%dT%H%M%S`): names are collision-free by construction, so parallel
branches never fight over "the next number" and a merge cannot produce two
migrations claiming the same slot.

## Who dials

On a locally owned data plane the applier connects as the administrator acting
as the schema owner (`base.db.pg_admin.local_owner_authority`): the OS user over
the home's owner-only Unix socket, custody-checked against the home's
postmaster, with `role=<owner>` as a startup option. Objects stay owner-owned,
privilege checks see only the owner's rights, the dial bypasses PgBouncer (the
session advisory lock survives) and carries no statement ceiling. The owner's
own login is never used. A remote-managed plane dials its provider URL directly
and unbounded instead.

## Only the gateway unit may apply

A cluster's schema belongs to the unit that owns its data plane. Every other host
in a split deployment points `AVA_DB_URL` at that same central Postgres, so
without a rule any of them could migrate it out from under the gateway — which
then boots into `CodeBehindSchema` and rejects every agent.

`apply_pending_migrations` therefore reads the cluster's identity from the DB
(`machine_units` rows with `serve_gateway`) and refuses unless the executing
checkout claims the same `(machine_name, home)`. The claim is the process's home
(`base.host.env.dotenv_boot.resolve_ava_home()`) together with the rule that a
home carrying its own `<home>/source` checkout is changed only by that checkout
(`home_checkout_error`): a process that inherited `AVA_HOME` from another cluster's
environment has that cluster's DB URL *and* its home, while still carrying its own
`migrations/`, and the checkout rule is what refuses it.

The check runs only when something is actually pending, so an agent-runner's
ordinary `ava start` — which calls this and applies nothing — is unaffected. An
empty `machine_units` means a fresh birth (step 2.5 migrates before step 3
registers the unit) and is allowed.

Checkout enumeration is Git-bound. Integer-keyed migration history is unsupported and fails before mutation.

Rationale and the rejected alternatives:
[2026-07-31-migrations-are-gateway-only](../../../../docs/decisions/2026-07-31-migrations-are-gateway-only.md).

## Reset generations

Frozen inventories, atomic convergence, restore requirements, and reset rollback
floors: [[base/deploy/schema/docs/reset-generations.ava.okf.md]].

## Version assertion is bidirectional

Every long-running process (gateway / agent-host / ops / labeler) calls
`base.deploy.schema.migrations.assert_schema_current(db_url)` at startup:

| Condition | Exception | Meaning |
|---|---|---|
| DB applied < code required | `SchemaVersionMismatch` | the normal "migration not run yet" |
| DB applied > code required | `CodeBehindSchema` | this host missed a rollout — local **code** is stale |

`CodeBehindSchema` refuses startup. No watchdog or automatic updater repairs the
code/schema mismatch; the operator must establish a compatible image and schema.

Startup schema connections dial through `base.db.connect_url()`, whose
transport posture includes a 5s `connect_timeout`, so dropped packets raise a
bounded connection failure rather than leaving startup blocked on the OS TCP
retransmission timeout.

## Notes

- There is **no standalone migrate command**. Pending migrations are applied as
  a step of `ava start`, early in boot — after Postgres is up and before the
  assertion — so any restart crossing a schema change catches the DB up.
- A migration that changes a trigger or function body must add an exercise line
  to `scripts/test_migrations_apply.sh`: `CREATE OR REPLACE FUNCTION` does **not**
  validate PL/pgSQL column references at apply time, so a body bug surfaces only
  when the trigger actually fires — one such bug passed lint, pytest, and the
  apply itself, and was caught only by a real cross-machine force-terminate in
  production.
- Review test for a new migration: running `db/schema.sql` on a fresh DB, and
  running the baseline plus all post-baseline migrations, must converge to the
  same schema.

Contributor steps and validation commands: [database migration guide](../../../../db/docs/migrations.md).

## Key Dependencies

- [[base/docs/base.ava.okf.md]] — the base-layer overview
- [[cli/docs/cli.ava.okf.md]] — `ava start` and the fleet update (`cli/fleet_update.py`), which apply and roll back
