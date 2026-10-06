---
type: doc
title: PG-Backup — Daily Local Postgres Backup
description: A daily local pg_dump driven by a gateway scheduler daemon — at the first wake after ``services.backup_hour`` (default 03:00) in cluster time, a custom-format full-database dump is made to $AVA_HOME/backups/db/ under a UTC-stamped name, keeping the newest ``services.backup_keep`` copies. Protects against bad migrations / accidental deletion / DB corruption, not against disk failure.
tags: []
---

# PG-Backup — Daily Local Postgres Backup

## What is it
A gateway-owned `pg-backup` scheduler daemon, declared by `ops/spec.py` as a
first-class `ServiceSpec`. It calls the dump logic after `services.backup_hour` (default 3 AM
**cluster** time — `AVA_TIMEZONE`, cluster-pinned) when the current cluster day
has no dump; a host down at 03:00 catches up at the next scheduler wake. Its
health reports last-success age, so the watchdog only supervises the scheduler
and is never delayed by a dump. The day boundary is the cluster's, not the
host's: reading a host timezone can make a current dump appear to be future.

## Core Responsibilities
- **Daily dump**: `pg_dump --format=custom --compress=zstd:3` full database (the custom archive compresses in-dump, including the LangGraph checkpoint tables that hold conversation history), then encrypts it to `$AVA_HOME/backups/db/<dbname>-YYYYMMDDTHHMMSSZ.dump.enc` — under path-only cluster identity, `$AVA_HOME` itself already uniquely locates the cluster, so dump directories no longer need a cluster token (historical dumps under old `backups/<cluster-name>/` are kept in place, not migrated).
- **UTC-stamped names**: the stamp is UTC with an explicit `Z`, so prune's oldest-first ordering is a total order over real instants. The grammar is `services/backup/artifact/names.py` (`<db>-<stamp>.dump.enc`, with `.dump.gz.enc` and a bare `.dump` also inside it); a name outside it, such as a pre-cutover wall-clock stamp, is never counted or pruned.
- **One passphrase resolution** (`services/backup/artifact/passphrase.py`): writers and every restore path use the pinned `$AVA_HOME/backups/logical-backup.passphrase`, minted at birth and independent of the cluster secret; it is backup-critical — see [[passphrase.ava.okf.md|Logical-backup passphrase]] for its lifecycle and escrow.
- **Atomic write, no plaintext at rest**: writes private plaintext and encrypted `.partial` files first, then publishes the encrypted artifact only after both commands succeed — half-finished dumps won't be mistaken for complete backups by due/prune logic or manual recovery. A failed or interrupted run removes its partials once its tool is killed and reaped. Every run, the scheduled worker's staged one included, first sweeps `backups/db/` under the backup lock: a dead run's partials, key files and cross-filesystem publish copies that no process holds open are removed (one still held by an orphaned writer is reported and waits), so a killed run's plaintext never outlives the next nightly dump.
- **Scheduled worker**: the dump uses a fixed worker and unique private staging
  (`services/backup/scheduler/worker.py`, see [[shutdown.ava.okf.md|Shutdown]]).
  The worker encrypts and publishes off-site; its controller validates zero exit,
  artifact name and SHA-256 before exclusive publication (a verified private
  copy across filesystems). Pruning respects the existing backup lock.
  Failure/cancellation sanitizes staging and reports cleanup errors with the
  private path. No durable custody, quarantine or blocked-kind retirement is
  required; see [[services/backup/scheduler/docs/scheduled-workers.ava.okf.md|Scheduled workers]].
- **Keep a week**: only manages dumps matching the managed-name grammar (`services/backup/artifact/names.py`); manually placed dumps in the same directory are never touched; deletes those beyond the newest `services.backup_keep` (default 7).
- **Timeout guard**: `_DUMP_TIMEOUT_S=60min` bounds pg_dump inside the scheduler; a stalled dump cannot freeze watchdog supervision.
- **Dump authority** (`dump_source`): a locally owned plane dumps as the administrator acting as the schema owner over the home's owner-only socket (`base.db.pg_admin`), custody-checked first and password-free, so no write-generation login is needed and a rollout revoking one cannot kill a dump; a remote-managed plane dumps through its provider URL (password in the child environment only). The restore drill recreates the capability groups `ava_gateway`/`ava_runner` the dump's grants name.

## Key Dependencies
- [[ava_root.ava.okf.md]] — probes and restarts the scheduler without executing the dump
- [[db.ava.okf.md]] — the dump target is the cluster's Postgres database

## Entry Points
- `services/backup/scheduler/daemon.py` — scheduled due-check, retry, and health state
- `services/backup/dump.py` — `run_backup()` dumps, publishes and prunes in-process; `run_backup(staging=...)` writes the scheduler worker's artifact into its private controls. `python -m services.backup.dump --publish-offsite ARTIFACT [--offsite-root PREFIX]` publishes one existing artifact and, like the scheduled leg, prints its result on stderr
- `services/backup/scheduler/worker.py` — the dump and logical restore-drill operation kinds, their sanitizers, and `commit_scheduled_backup()`
- `services/backup/artifact/intermediates.py` — `sweep_closed_partials()`, the closed-intermediate sweep every backup run applies to the backup directory
- `services/backup/artifact/names.py` — the managed-dump name grammar and the off-site namespace root (`ava-logical`)
- `services/backup/artifact/offsite.py` — `publish()`: the best-effort OSS publish of one finished artifact (multipart, per-part `Content-MD5`, ETag-chain verification, forbid-overwrite on completion only, adopt-after-crash)
- The daily dump is this package's recovery point; point-in-time recovery exists only while the optional WAL-G path ([[walg.ava.okf.md|WAL-G]]) is on — it ships WAL and takes daily base backups, and `ava backup walg restore` restores one into a directory you name, proved weekly by its recovery drill (see `docs/conventions/disaster-recovery.md`). Nothing here deletes a remote object.

## Notes
- Gateway capability only; `ava start --disable-service pg-backup` prevents its scheduler session from starting and watchdog revival respects the same marker.
- Protects against bad migrations / accidental deletion / DB corruption and makes a best-effort encrypted off-site publish to OSS before local commit and pruning (`services/backup/artifact/offsite.py`, objects under `ava-logical/`; `AVA_BACKUP_OFFSITE_ENDPOINT`, `AVA_BACKUP_OFFSITE_BUCKET` and `AVA_BACKUP_OFFSITE_CREDENTIALS_FILE`). A home without all three skips the leg with one INFO line; an unavailable store or a failed upload leaves the local artifact intact and logs the cause. The bucket must stay versioning-off, since a versioned bucket ignores forbid-overwrite. See `future/infra/pg-backup.md`.
- Restore: follow `.agents/skills/ava-guide/operations/references/db-restore.md` to decrypt before `pg_restore --clean --if-exists`.
[[shutdown.ava.okf.md|Shutdown ownership]]
