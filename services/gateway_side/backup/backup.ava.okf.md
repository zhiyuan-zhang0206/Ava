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
- **UTC-stamped names**: the stamp is UTC with an explicit `Z`, so prune's oldest-first ordering is a total order over real instants. Pre-update snapshots carry a `.pre-update` kind segment (`<db>-<ts>.pre-update.dump.enc`). Legacy `<dbname>-YYYYMMDD-HHMMSS.dump` names (host wall clock, no offset) stay managed and are read in cluster time, so a week of pre-cutover dumps still prunes out instead of stranding; legacy `<db>-<ts>.dump.gz.enc` artifacts (pre-double-gzip-removal) also stay managed and restorable.
- **Atomic write, no plaintext at rest**: writes private plaintext and encrypted `.partial` files first, then publishes the encrypted artifact only after both commands succeed — half-finished dumps won't be mistaken for complete backups by due/prune logic or manual recovery. A failed or interrupted run removes its partials once its tool is killed and reaped; the next run under the backup lock removes a dead run's partials and key files that no process holds open (one still held by an orphaned writer is reported and waits).
- **Owned operation**: the scheduled dump runs in one directly owned worker group (`services/backup_scheduler/worker.py`, see [[shutdown.ava.okf.md|Shutdown ownership]]). The worker writes and best-effort publishes off-site inside `$AVA_HOME/backups/operations/dump/`; the controller links the exact artifact into the backup directory (a verified private copy when that directory is on another filesystem) only after the group closed and the worker exited 0 with a matching SHA-256, then prunes unless another backup holds the lock. A failed or cancelled run is quarantined under `$AVA_HOME/backups/quarantine/` with plaintext removed (a complete encrypted artifact from an interrupted upload is kept) and the next run proceeds; unproven closure blocks dumps until `ava pitr operations retire` ([[services/pitr/operation-custody.ava.okf.md|Operation custody]]).
- **Keep a week, plus the newest update snapshot**: only manages dumps matching the shared managed-name grammar (`services/pitr/logical_dump_names`, the same grammar the remote retention planner classifies by); manually placed dumps in the same directory are never touched; deletes those beyond the newest `services.backup_keep` (default 7) **daily** dumps, and keeps the newest `<db>-<ts>.pre-update.dump.enc` snapshot (migration-bearing updates with PITR disabled) in its own slot.
- **Timeout guard**: `_DUMP_TIMEOUT_S=60min` bounds pg_dump inside the scheduler; a stalled dump cannot freeze watchdog supervision.

## Key Dependencies
- [[watchdog.ava.okf.md]] — probes and restarts the scheduler without executing the dump
- [[db.ava.okf.md]] — the dump target is the cluster's Postgres database

## Entry Points
- `services/backup_scheduler/daemon.py` — scheduled due-check, retry, and health state
- `services/backup.py` — `run_backup()` dumps, publishes and prunes in-process for snapshot callers; `run_backup(staging=...)` writes the scheduler worker's artifact into its private controls
- `services/backup_scheduler/worker.py` — the dump and logical restore-drill operation kinds, their sanitizers, and `commit_scheduled_backup()`
- `services/gateway_side/backup/snapshot.py` — `create_pre_update_snapshot()`
  creates a local recovery dump; `create_pre_activation_snapshot()` creates the
  operation-scoped PITR logical floor with its existing off-site publication.
  Both retain the reentrant backup lock through `verify_snapshot()`, which
  decrypts the artifact, handles legacy gzip and requires a nonempty
  `pg_restore --list` table of contents. Callers provide progress sinks;
  lock waits and dump stages remain bounded and visible. Git/schema policy
  stays with the caller, outside this backup-owned module.
- Physical PITR — WAL archiving, base chains, the gated GCS uploader, and the dry-run retention planner over both this module's `ava-logical/` pool and the PITR prefix — is its own node: [[services/pitr/pitr.ava.okf.md|Physical PITR]]; every layer is default-off, and remote deletion stays impossible until an operator arms the deletion role.

## Notes
- Gateway capability only; `ava start --disable-service pg-backup` prevents its scheduler session from starting and watchdog revival respects the same marker.
- Protects against bad migrations / accidental deletion / DB corruption and makes a best-effort encrypted off-site publish through the shared backup store contract before local commit and pruning (`services.pitr.store_factory` — the same backend switch as the physical PITR plane, objects under `ava-logical/`); an unavailable store leaves the local artifact intact. See `future/infra/pg-backup.md`.
- Restore: follow `.agents/skills/operating-ava-cluster/references/db-restore.md` to decrypt before `pg_restore --clean --if-exists`.
[[shutdown.ava.okf.md|Shutdown ownership]]
