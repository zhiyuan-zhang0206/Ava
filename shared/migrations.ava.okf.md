---
type: doc
title: Schema Migrations (baseline + deltas)
description: '`db/schema.sql` is the squashed baseline and the rollback floor; `migrations/YYYYMMDDTHHMMSS_<name>.sql` are post-baseline deltas tracked as an applied SET, not a high-water integer. `shared/migrations.py` applies them and asserts version in both directions at every daemon start.'
tags:
- shared
- library
- database
---

# Schema Migrations (baseline + deltas)

The internal verified-release argument on the real `cmd_start` entry validates
the operation receipt before setup, source repair, or converge. A migration
receipt alone cannot authorize service cutover: start currently refuses until
prepared service closure, bootable LKG and the all-writer barrier are implemented.
The existing start migration helper accepts the same typed context and preserves
checkpoint dependency/schema checks. No public CLI activation option is exposed.

## Two places, one schema

- **`db/schema.sql`** — the squashed **baseline**: the full current schema a
  fresh DB bootstraps from, and the source of truth for what the schema is now.
  `shared.cluster.provision_database` applies it to each cluster's own database
  (created owned by the cluster's role, applied *as* that role so every object
  is role-owned); tests build standalone DBs the same way. It stamps
  one sentinel row `00000000T000000_baseline` into `schema_migrations`.
  **A schema change must be reflected here in the same commit.**
- **`migrations/YYYYMMDDTHHMMSS_<kebab-name>.sql`** — post-baseline deltas, each
  paired with a `.down.sql`.

## Applied set, not a version number

`shared.migrations.apply_pending_migrations` applies every file whose **name** is
not yet in the DB's applied set, in name (≈ chronological) order. Each file runs
as a single transaction; the **runner INSERTs the file's name into
`schema_migrations`** on completion (migration files must NOT insert
themselves — a self-insert would collide on the primary key) — `name TEXT
PRIMARY KEY`, a **set**, not a high-water integer.

That is the whole point of the timestamp prefix (second-precision UTC,
`date -u +%Y%m%dT%H%M%S`): names are collision-free by construction, so parallel
branches never fight over "the next number" and a merge cannot produce two
migrations claiming the same slot.

## Only the gateway unit may apply

A cluster's schema belongs to the unit that owns its data plane. Every other host
in a split deployment points `AVA_DB_URL` at that same central Postgres, so
without a rule any of them could migrate it out from under the gateway — which
then boots into `CodeBehindSchema` and rejects every agent.

`apply_pending_migrations` therefore reads the cluster's identity from the DB
(`machine_units` rows with `serve_gateway`) and refuses unless the executing
checkout claims the same `(machine_name, home)`. The claim comes from
`shared.dotenv_boot.checkout_anchored_home()` — `resolve_ava_home()` **minus**
the `AVA_HOME` override — because a process that inherited `AVA_HOME` from
another cluster's environment has that cluster's DB URL *and* its home, while
still carrying its own `migrations/`. An unanchored checkout is refused: its
`~/.ava` fallback is a default, not a claim of ownership.

The check runs only when something is actually pending, so an agent-runner's
ordinary `ava start` — which calls this and applies nothing — is unaffected. An
empty `machine_units` means a fresh birth (step 2.5 migrates before step 3
registers the unit) and is allowed.

An explicit verified-release transition context is stricter: it requires the
current non-settling rollout lease identity, canonical unit-owned generation,
manifest-bound packaged SQL inventory, and a nonempty matching gateway roster
before any legacy conversion or migration. It does not obtain authority from an
environment override. Ordinary checkout enumeration remains Git-bound. Legacy
integer-history conversion also checks gateway authority before its first write.

Installed wheel startup can compare schema versions without Git using the loaded
distribution's hash-declared SQL members. The distribution must correspond to the
loaded module, must not be editable, and changed or undeclared SQL is rejected.
This RECORD check is package integrity for read-only comparison, not deployment
authority: an ordinary unanchored wheel still cannot migrate the database.

The existing CLI transition helper records one secret-free candidate receipt per
lease acquisition under the unit's `run/` directory. It validates operation,
gateway ownership and the complete image before atomically writing a 0600
receipt. Re-recording identical evidence is idempotent; a different candidate
for that acquisition is refused. Loading revalidates the current lease, target,
platform, image and gateway tuple. This journal neither selects an image nor
authorizes service activation independently of the deployment operation.

Rationale and the rejected alternatives:
[2026-07-31-migrations-are-gateway-only](../decisions/2026-07-31-migrations-are-gateway-only.md).

## Reversibility

The baseline is the **rollback floor**, so everything above it must be
reversible: every post-baseline migration ships a paired `.down.sql`.
`scripts/lint_migrations.py` statically enforces the filename format, name
uniqueness, up/down pairing, and that `db/schema.sql` stamps the sentinel. There
is deliberately **no** continuity / next-number / cross-branch-collision check —
timestamp names make those checks meaningless.

A lossy operation (drop column/table, destructive transform) goes
**expand-contract**: the drop is its own later migration, decoupled from the
commit that stopped using the data, so any single upgrade's migration set stays
reversible.

A rollback that would cross the baseline cannot be performed — resetting code
under a newer schema would put every daemon in `CodeBehindSchema`, so the
recovery path leaves code and schema consistent on the new revision for
fix-forward and alerts loudly instead.

## Version assertion is bidirectional

Every long-running process (gateway / agent-host / ops / labeler) calls
`shared.migrations.assert_schema_current(db_url)` at startup:

| Condition | Exception | Meaning |
|---|---|---|
| DB applied < code required | `SchemaVersionMismatch` | the normal "migration not run yet" |
| DB applied > code required | `CodeBehindSchema` | this host missed a rollout — local **code** is stale |

`CodeBehindSchema` is what makes an offline runner self-heal: the watchdog sees
it and spawns an `ava cluster update`, which takes the self-update branch on a runner.

Startup schema connections use `shared.db.PG_KEEPALIVE_KWARGS`, including a 5s
`connect_timeout`, so dropped packets raise a bounded connection failure rather
than leaving startup blocked on the OS TCP retransmission timeout.

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

## Key Dependencies

- [[shared.ava.okf.md]] — the shared-layer overview
- [[../cli/cli.ava.okf.md]] — `ava start` / `ava cluster update`, which apply and roll back
