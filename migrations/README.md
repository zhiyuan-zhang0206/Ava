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

## Current baseline (2026-09-23)

The 101 deltas through `20260921T211400_watcher-agent-notify` are folded into
`db/schema.sql`. Fresh databases stamp the baseline sentinel and
`20260923T031516_schema-baseline`; the retired history's seed rows are absent.
Subsequent deltas remain paired and must also be reflected in the current schema.

`shared/migration_history.py` keeps the exact frozen inventories for this reset
and the 2026-08-14 reset (59 names). The earlier inventory remains necessary
because restores before 2026-08-14 have not been ruled out. Before deleting any
tracking rows, the runner refuses partial generations. A database without the
current anchor must carry the entire 101-name predecessor inventory; an old
baseline-only restore cannot masquerade as a fresh database.

Upgrade older restores through the release immediately before the applicable
reset. Integer-keyed history is unsupported; restore it under its matching
release before advancing. Current live databases already use the applied set.

The current anchor is a rollback floor. Rolling back across it is refused before
any down migration executes: the folded history includes strict DDL, so replaying
an older release cannot safely restore it. Recover across the boundary with a
matching pre-reset database backup, or fix forward. Merge does not authorize a
runtime rollout or a restore.
