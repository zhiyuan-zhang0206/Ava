# Down-failure drill runbook

## Purpose

Prove that when a `.down.sql` fails mid-rollback:

1. the whole rollback aborts atomically: the schema is unchanged, not partial;
2. the recovery message names the verified pre-update data snapshot; and
3. the snapshot restores with `pg_restore` into an isolated database.

This drill covers a failed gateway-local update after a migration has applied.
It does not test normal forward migration success in isolation.

## Prerequisites

- Use a worktree/dev cluster installed with `scripts/install.sh --worktree`, or
  use staging with explicit approval.
- Have a real local Postgres plus working `pg_dump` and `pg_restore` binaries.
- Run from the checkout branch under test.
- Choose a known table and a known row count for the restore verification.
- Create a throwaway database for the restore before starting the drill.

Never run this drill against the production cluster. The restore command uses
`--clean --if-exists` and replaces the contents of the database it targets.

## Scenario setup

1. Create a timestamped migration, for example
   `migrations/<ts>_drill-down-failure.sql`, whose up file creates a drill table:

   ```sql
   CREATE TABLE drill_t (id bigint PRIMARY KEY, note text NOT NULL);
   ```

2. Add the paired `<ts>_drill-down-failure.down.sql` with a deliberate runtime
   failure after the up migration exists. Do not use a top-level `DROP TABLE`
   without `IF EXISTS`: migration lint rejects it. A missing column passes that
   lint while still failing at runtime:

   ```sql
   ALTER TABLE drill_t DROP COLUMN no_such_column;
   ```

3. Commit the migration. Confirm the target branch contains the migration and
   that the cluster's current `schema_migrations` set does not. The pre-update
   snapshot is created only for this migration-bearing transition; a later
   code-only update intentionally skips it.

4. Arrange a deterministic failure of the target's fresh `ava start` after its
   migrations apply, then run `ava cluster update`. A controlled REPL call to
   `_recover_gateway_local` is acceptable only when it receives the snapshot
   path captured by this migration-bearing update. The mechanical fault is
   environment-specific; the observables below are the acceptance criteria.

5. Capture the pre-update snapshot path printed by recovery. It must be under
   `<home>/backups/db/` and use the managed `<db>-<ts>.dump.enc` naming
   convention (legacy `.dump.gz.enc` names remain managed during transition).

## Expected observables

After the intentional start failure reaches recovery, verify all of these:

- `schema_migrations` still contains the drill migration name:

  ```sql
  SELECT name FROM schema_migrations ORDER BY name;
  ```

  This proves the broken down migration did not commit a partial rollback.

- `drill_t` still exists. Its failed down file did not remove part of the
  migrated schema.

- The recovery log includes both `MANUAL INTERVENTION` and the exact
  pre-update snapshot path, followed by the restore command shape with literal
  `<db_url>` and a link to this drill.

- The recovery message says that the down migration rollback aborted
  atomically. Do not reset code beneath that schema; follow the stated
  fix-forward or restore decision instead.

## Restore verification

1. Work only in a private temporary directory. Managed artifacts are encrypted
   custom-format dumps; decrypt them using the same semantics as
   [`services.backup.decrypt_artifact`](../services/backup.py): the key file
   contains the SHA-256 of the cluster secret, is mode `0600`, and is never put
   on the command line. The decrypt command shape is:

   ```bash
   openssl enc -d -aes-256-cbc -pbkdf2 -salt -kfile <sha256-cluster-secret-key-file> \
     -in <snapshot.dump.enc> -out <snapshot.dump>
   ```

2. If the artifact predates the 2026-08-27 double-gzip removal (name
   `.dump.gz.enc`), gunzip the decrypted custom dump:

   ```bash
   gzip --decompress --stdout <snapshot.dump> > <snapshot.dump.raw> && mv <snapshot.dump.raw> <snapshot.dump>
   ```

3. Restore only into the throwaway database created for this drill:

   ```bash
   pg_restore --clean --if-exists -d <isolated-db-url> <snapshot.dump>
   ```

4. Connect to the throwaway database and verify its `schema_migrations` rows.
   They must match the pre-update state, not include the drill migration.

5. Verify the selected known table has its expected rows. Record the observed
   count rather than accepting a successful restore exit code as proof.

6. Point an isolated Ava process at the restored database and run an
   `ava status`-level smoke check. Confirm it can read the restored schema and
   is not accidentally connected to the worktree or production database.

## Cleanup

1. Remove the drill migration and its broken down file from the branch.
2. Drop `drill_t` only after the branch no longer references the migration.
3. Drop the throwaway restore database.
4. Remove the temporary decrypted dump and the temporary key file.
5. Remove any temporary start-failure mechanism before the next update.
6. Confirm the worktree cluster is back on its intended revision and `ava
   status` is healthy.

## References

- [`shared/migrations.ava.okf.md`](../shared/migrations.ava.okf.md) —
  expand-contract requirements for lossy operations.
- [`cli/commands/_update_recover.py`](../cli/commands/_update_recover.py) —
  failed-rollout recovery and manual-intervention diagnostics.
- [`cli/commands/_update_git.py`](../cli/commands/_update_git.py) — verified
  pre-update snapshot creation.
