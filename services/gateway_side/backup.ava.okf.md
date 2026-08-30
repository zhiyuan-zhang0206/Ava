---
type: doc
title: PG-Backup — Daily Local Postgres Backup
description: A daily local pg_dump driven by a gateway scheduler daemon — at the first wake after BACKUP_HOUR in cluster time, a custom-format full-database dump is made to $AVA_HOME/backups/db/ under a UTC-stamped name, keeping the newest BACKUP_KEEP copies. Protects against bad migrations / accidental deletion / DB corruption, not against disk failure.
tags: []
---

# PG-Backup — Daily Local Postgres Backup

## What is it
A gateway-owned `pg-backup` scheduler daemon, declared by `ops/spec.py` as a
first-class `ServiceSpec`. It calls the dump logic after `BACKUP_HOUR` (3 AM
**cluster** time — `AVA_TIMEZONE`, cluster-pinned) when the current cluster day
has no dump; a host down at 03:00 catches up at the next scheduler wake. Its
health reports last-success age, so the watchdog only supervises the scheduler
and is never delayed by a dump. The day boundary is the cluster's, not the
host's: reading a host timezone can make a current dump appear to be future.

## Core Responsibilities
- **Daily dump**: `pg_dump --format=custom --compress=zstd:3` full database (the custom archive compresses in-dump, including the LangGraph checkpoint tables that hold conversation history), then encrypts it to `$AVA_HOME/backups/db/<dbname>-YYYYMMDDTHHMMSSZ.dump.enc` — under path-only cluster identity, `$AVA_HOME` itself already uniquely locates the cluster, so dump directories no longer need a cluster token (historical dumps under old `backups/<cluster-name>/` are kept in place, not migrated).
- **UTC-stamped names**: the stamp is UTC with an explicit `Z`, so prune's oldest-first ordering is a total order over real instants. Pre-update snapshots carry a `.pre-update` kind segment (`<db>-<ts>.pre-update.dump.enc`). Legacy `<dbname>-YYYYMMDD-HHMMSS.dump` names (host wall clock, no offset) stay managed and are read in cluster time, so a week of pre-cutover dumps still prunes out instead of stranding; legacy `<db>-<ts>.dump.gz.enc` artifacts (pre-double-gzip-removal) also stay managed and restorable.
- **Atomic write**: writes private plaintext and encrypted `.partial` files first, then publishes the encrypted artifact only after both commands succeed — half-finished dumps won't be mistaken for complete backups by due/prune logic or manual recovery.
- **Keep a week, plus the newest update snapshot**: only manages dumps matching this module's naming convention (`_NAME_RE`); manually placed dumps in the same directory are never touched; deletes those beyond the newest `BACKUP_KEEP=7` **daily** dumps, and keeps the newest `<db>-<ts>.pre-update.dump.enc` snapshot (`ava cluster update` writes one per migration-bearing rollout) in its own slot.
- **Timeout guard**: `_DUMP_TIMEOUT_S=60min` bounds pg_dump inside the scheduler; a stalled dump cannot freeze watchdog supervision.

## Key Dependencies
- [[watchdog.ava.okf.md]] — probes and restarts the scheduler without executing the dump
- [[db.ava.okf.md]] — the dump target is the cluster's Postgres database

## Entry Points
- `services/backup_scheduler/daemon.py` — scheduled due-check, retry, and health state
- `services/backup.py:run_backup()` — actual dump + prune
- `cli/commands/_converge_pitr.py` — publishes the disabled-by-default physical-backup layout and stable archive shim; it does not alter PostgreSQL
- `services/pitr/archive_shim.py` — stdlib-only atomic local WAL spool entry point, reserved for a later archive-mode rollout
- `services/pitr/activation_state.py` — strict schema-v3 atomic activation record; CAS transitions persist config digests, restart handoffs, exact WAL ACK/viewer evidence, and candidate/protected digests while preserving `started_at`
- `services/pitr/uploader_daemon.py` — disabled-by-default single-worker GCS uploader; it verifies immutable conditional creates before publishing a durable local ACK
- `services/pitr/base_scheduler_daemon.py` — separately gated weekly scheduler for physical base candidates and generation-pinned restore proofs; both gates default off and it never deletes remote data
- `services/pitr/retention_planner.py` — default-off local dry-run planner; strictly joins local ACK and viewer-only remote immutable identities, atomically records a canonical fail-closed N=2 plan, and exposes only fresh counts/bytes through scheduler health, with no remote-delete boundary; unprotected or cross-timeline evidence blocks eligibility
- `cli/commands/pitr.py` — read-only `ava pitr retention inspect` view of the latest durable local plan

## Notes
- Gateway capability only; `ava start --disable-service pg-backup` prevents its scheduler session from starting and watchdog revival respects the same marker.
- Protects against bad migrations / accidental deletion / DB corruption and makes a best-effort encrypted off-site publish through the shared backup store contract before pruning (`services.pitr.store_factory` — the same backend switch as the physical PITR plane, objects under `ava-logical/`); an unavailable store leaves the local artifact intact. See `future/infra/pg-backup.md`.
- Restore: follow `.agents/skills/operating-ava-cluster/references/db-restore.md` to decrypt before `pg_restore --clean --if-exists`.
- Physical PITR is disabled by default: converge publishes a private
  per-home spool and a source-independent, self-checked shim, while
  `AVA_PITR_ENABLED` defaults false and PostgreSQL `archive_mode` stays untouched.
  `ava cluster pitr activate` first validates that shadow posture and creates a
  verified encrypted logical snapshot, then durably resumes through the existing
  whole-cluster restart path. Protection requires exact PostgreSQL/local ACK/
  viewer WAL evidence, an operation-scoped base candidate, and its isolated
  restore proof. Rollback restores frozen settings through the same restart seam
  and never deletes backup data.
  A local archived segment is not a remote ACK. When explicitly enabled, the
  GCS uploader encrypts each spooled segment, conditionally creates one immutable
  object, verifies its generation/CRC32C/metadata, and only then fsyncs an ACK
  before removing local staging and spool files. Base chains, migration gate,
  and isolated physical restore arrive in follow-up PRs; logical
  daily/pre-update dumps remain the active recovery contract throughout.
  `AVA_PITR_BASE_BACKUP_ENABLED` is a second, default-off gate: enabling WAL
  upload alone cannot accidentally start a multi-GiB weekly base. A candidate
  is born as one plain `pg_basebackup -Fp -X none` tree under the shared backup
  lock, locally checked with `pg_verifybackup`, then uploaded outside the lock
  as a deterministic canonical tar → zstd → AEAD stream. The stream is read
  twice (identity preflight then upload) but no complete tar or ciphertext is
  written locally. Candidates are explicitly `protected=false`; generation-
  A third default-off restore-proof gate downloads each exact object generation
  once into owned quarantine, authenticates locally, replays through an exact
  WAL allowlist in a sibling PostgreSQL instance, verifies stable fingerprints,
  and only then publishes a new immutable `protected=true` manifest. It never
  edits the candidate, touches live PGDATA, selects a latest object, or deletes
  remote data. The optional `AVA_PITR_RETENTION_PLANNER_ENABLED` slice computes
  only a local dry-run plan: latest two protected chains by capture identity,
  every unprotected candidate pinned, and continuous ACKed WAL/history from the
  oldest retained base. Any malformed, missing, forked, generation-ambiguous or
  concurrently changing evidence blocks all eligibility. It has no delete API,
  credential or production-enable path; remote execution remains future work.
  The second gate also requires an explicit local least-privilege replication
  URL; the ordinary cluster owner remains `NOSUPERUSER` without `REPLICATION`.
