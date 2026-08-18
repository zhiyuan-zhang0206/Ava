# migrations/

Post-baseline schema deltas. Each is a pair:

```
YYYYMMDDTHHMMSS_<kebab-name>.sql        -- forward
YYYYMMDDTHHMMSS_<kebab-name>.down.sql   -- reverse (mandatory)
```

- **Timestamp prefix** = second-precision UTC (`date -u +%Y%m%dT%H%M%S`). It
  makes names collision-free without a coordinating counter, so parallel
  branches never fight over the "next number". Names sort ≈ chronologically;
  `apply_pending_migrations` applies every file whose name is not yet in the
  DB's applied set, in name order.
- **Data-destroying downs are guarded.** A down that would destroy data with
  no surviving source (e.g. dropping a table whose dual-write mirrors were
  removed) raises unless the operator confirms first; the guard and the exact
  confirmation command are declared at the top of the down file. `rollback_to`
  / `apply_down` do not bypass guards — a rollback crossing such a migration
  stops at it with the file's message, and the operator dumps what must be
  kept, confirms, and re-runs. The reversibility rule applies to schema shape;
  a value-destroying down may be deliberately best-effort or refused.
- **`.down.sql` is mandatory** for every file here — the baseline is the
  rollback floor, so everything above it must be reversible (enforced by
  `scripts/lint_migrations.py`).
- The current full schema lives in **`db/schema.sql`** (the squashed baseline a
  fresh DB bootstraps from); a new migration must also reflect its change there.

## v0.1.0 baseline (2026-08-14)

The pre-release migration history (59 timestamped deltas) was squashed into
`db/schema.sql` and reset at the v0.1.0 public release. `20260814T235959_v010-baseline.sql`
is the single migration that history collapses to — an intentionally empty
anchor (schema is 100% in `db/schema.sql`), so a fresh install's applied set is
exactly `{baseline, v010-baseline}` and future deltas sort after it.

A cluster upgrading across the reset converges automatically:
`apply_pending_migrations` drops applied-set rows whose files no longer exist
(they were folded into the baseline), then applies the v0.1.0 anchor — no
manual DB surgery, no downtime window. A cluster that later rolls back to
pre-reset code self-heals: the older code re-applies its idempotent migration
files against the already-current schema and restores the names.
