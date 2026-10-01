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
- **UTC-stamped names**: the stamp is UTC with an explicit `Z`, so prune's oldest-first ordering is a total order over real instants. The grammar is `services/gateway_side/backup/names.py` (`<db>-<stamp>.dump.enc`, with `.dump.gz.enc` and a bare `.dump` also inside it); a name outside it, such as a pre-cutover wall-clock stamp, is never counted or pruned.
- **One passphrase resolution** (`services/gateway_side/backup/passphrase.py`): writers and every restore path use the pinned `$AVA_HOME/backups/logical-backup.passphrase`, minted at birth and independent of the cluster secret; it is backup-critical — see [[passphrase.ava.okf.md|Logical-backup passphrase]] for its lifecycle and escrow.
- **Atomic write, no plaintext at rest**: writes private plaintext and encrypted `.partial` files first, then publishes the encrypted artifact only after both commands succeed — half-finished dumps won't be mistaken for complete backups by due/prune logic or manual recovery. A failed or interrupted run removes its partials once its tool is killed and reaped. Every run, the scheduled worker's staged one included, first sweeps `backups/db/` under the backup lock: a dead run's partials, key files and cross-filesystem publish copies that no process holds open are removed (one still held by an orphaned writer is reported and waits), so a killed run's plaintext never outlives the next nightly dump.
- **Owned operation**: the scheduled dump runs in one directly owned worker group (`services/backup_scheduler/worker.py`, see [[shutdown.ava.okf.md|Shutdown ownership]]). The worker writes and best-effort publishes off-site inside `$AVA_HOME/backups/operations/dump/`; the controller links the exact artifact into the backup directory (a verified private copy, held open until it is linked, when that directory is on another filesystem) only after the group closed and the worker exited 0 with a matching SHA-256, then prunes unless another backup holds the lock. A failed or cancelled run is quarantined under `$AVA_HOME/backups/quarantine/logical-dump/` with plaintext removed (a complete encrypted artifact from an interrupted upload is kept) and the next run proceeds; unproven closure blocks dumps until `ava backup operations retire` ([[services/backup_scheduler/docs/operation-custody.ava.okf.md|Operation custody]]).
- **Keep a week**: only manages dumps matching the managed-name grammar (`services/gateway_side/backup/names.py`); manually placed dumps in the same directory are never touched; deletes those beyond the newest `services.backup_keep` (default 7).
- **Timeout guard**: `_DUMP_TIMEOUT_S=60min` bounds pg_dump inside the scheduler; a stalled dump cannot freeze watchdog supervision.
- **Dump authority** (`dump_source`): a locally owned plane dumps as the administrator acting as the schema owner over the home's owner-only socket (`base.db.pg_admin`), custody-checked first and password-free, so no write-generation login is needed and a rollout revoking one cannot kill a dump; a remote-managed plane dumps through its provider URL (password in the child environment only). The restore drill recreates the capability groups `ava_gateway`/`ava_runner` the dump's grants name.

## Key Dependencies
- [[watchdog.ava.okf.md]] — probes and restarts the scheduler without executing the dump
- [[db.ava.okf.md]] — the dump target is the cluster's Postgres database

## Entry Points
- `services/backup_scheduler/daemon.py` — scheduled due-check, retry, and health state
- `services/backup.py` — `run_backup()` dumps, publishes and prunes in-process; `run_backup(staging=...)` writes the scheduler worker's artifact into its private controls. `python -m services.backup --publish-offsite ARTIFACT [--offsite-root PREFIX]` publishes one existing artifact and, like the scheduled leg, prints its result on stderr
- `services/backup_scheduler/worker.py` — the dump and logical restore-drill operation kinds, their sanitizers, and `commit_scheduled_backup()`
- `services/gateway_side/backup/intermediates.py` — `sweep_closed_partials()`, the closed-intermediate sweep every backup run applies to the backup directory
- `services/gateway_side/backup/names.py` — the managed-dump name grammar and the off-site namespace root (`ava-logical`)
- `services/gateway_side/backup/offsite.py` — `publish()`: the best-effort OSS publish of one finished artifact (multipart, per-part `Content-MD5`, ETag-chain verification, forbid-overwrite on completion only, adopt-after-crash)
- Physical PITR (WAL archiving, base chains, a dry-run retention planner that also classifies this module's `ava-logical/` pool) is a separate, default-off system with its own node; the daily backup does not import it, and remote deletion stays impossible until an operator arms that system's deletion role.

## Notes
- Gateway capability only; `ava start --disable-service pg-backup` prevents its scheduler session from starting and watchdog revival respects the same marker.
- Protects against bad migrations / accidental deletion / DB corruption and makes a best-effort encrypted off-site publish to OSS before local commit and pruning (`services/gateway_side/backup/offsite.py`, objects under `ava-logical/`; `AVA_BACKUP_OFFSITE_ENDPOINT`, `AVA_BACKUP_OFFSITE_BUCKET` and `AVA_BACKUP_OFFSITE_CREDENTIALS_FILE`). A home without all three skips the leg with one INFO line; an unavailable store or a failed upload leaves the local artifact intact and logs the cause. The bucket must stay versioning-off, since a versioned bucket ignores forbid-overwrite. See `future/infra/pg-backup.md`.
- Restore: follow `.agents/skills/operating-ava-cluster/references/db-restore.md` to decrypt before `pg_restore --clean --if-exists`.
[[shutdown.ava.okf.md|Shutdown ownership]]
