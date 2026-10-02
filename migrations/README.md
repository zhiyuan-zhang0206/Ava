# migrations/

Post-baseline schema deltas, one forward file each:

```
YYYYMMDDTHHMMSS_<kebab-name>.sql
```

- **Timestamp prefix** = second-precision UTC (`date -u +%Y%m%dT%H%M%S`). It
  makes names collision-free without a coordinating counter, so parallel
  branches never fight over the "next number". Names sort ≈ chronologically;
  `apply_pending_migrations` applies every file whose name is not yet in the
  DB's applied set, in name order.
- **No down migrations.** A mistake is fixed forward by a new migration, and
  lossy operations go expand-contract (the drop is its own later migration,
  after the code that stopped using the object has shipped).
- **A merged migration is immutable.** A file already on main is never edited,
  deleted or renamed — only new files are added
  (`scripts/content_lint/lint_migrations.py`, against the merge-base with
  `origin/main`). A DB that applied it never re-runs it, so changing the file
  forks what a fresh DB builds from what applied DBs hold.
- The current full schema lives in **`db/schema.sql`** (the squashed baseline a
  fresh DB bootstraps from); a new migration must also reflect its change there.

## Current baseline (2026-09-23)

The 101 deltas through `20260921T211400_watcher-agent-notify` are folded into
`db/schema.sql`. Fresh databases stamp the baseline sentinel and
`20260923T031516_schema-baseline`; the retired history's seed rows are absent.
Subsequent deltas must also be reflected in the current schema.

`base/deploy/schema/migration_history.py` keeps the exact frozen inventories for this reset
and the 2026-08-14 reset (59 names). The earlier inventory remains necessary
because restores before 2026-08-14 have not been ruled out. Before deleting any
tracking rows, the runner refuses partial generations. A database without the
current anchor must carry the entire 101-name predecessor inventory; an old
baseline-only restore cannot masquerade as a fresh database.

Upgrade older restores through the release immediately before the applicable
reset. Integer-keyed history is unsupported; restore it under its matching
release before advancing. Current live databases already use the applied set.

Replaying an older release across the current anchor cannot safely restore the
folded strict DDL. Recover across the boundary with a matching pre-reset database
backup, or fix forward. Merge does not authorize a
runtime rollout or a restore.
