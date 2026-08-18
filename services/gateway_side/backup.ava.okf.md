---
type: doc
title: PG-Backup — Daily Local Postgres Backup
description: A daily local pg_dump driven by the watchdog tick — on the gateway host, each day at the first tick after BACKUP_HOUR, a custom-format full-database dump is made to $AVA_HOME/backups/db/, keeping the latest copy only. Protects against bad migrations / accidental deletion / DB corruption, not against disk failure.
tags: []
---

# PG-Backup — Daily Local Postgres Backup

## What is it
A daily local Postgres backup that piggybacks on the watchdog tick — not an independent daemon, but `maybe_run_daily_backup()` as a pseudo healthcheck (named `pg-backup`, no ServiceSpec) manually appended to the tick tail by the gateway watchdog's `_checks_for_capability`. Called once per tick, it only actually runs after `BACKUP_HOUR` (local 3 AM) today and if no dump has been done today yet; a host down at 03:00 will catch up on the next tick, rather than skipping the day.

## Core Responsibilities
- **Daily dump**: `pg_dump --format=custom` full database to `$AVA_HOME/backups/db/<dbname>-YYYYMMDD-HHMMSS.dump` — under path-only cluster identity, `$AVA_HOME` itself already uniquely locates the cluster, so dump directories no longer need a cluster token (historical dumps under old `backups/<cluster-name>/` are kept in place, not migrated).
- **Atomic write**: writes `.partial` first, renames only after `pg_dump` succeeds — half-finished dumps won't be mistaken for complete backups by due/prune logic or manual recovery.
- **Keep 1 copy**: only manages dumps matching this module's naming convention (`_NAME_RE`); manually placed dumps in the same directory are never touched; deletes those exceeding the latest `BACKUP_KEEP=1`.
- **Timeout guard**: `_DUMP_TIMEOUT_S=30min` bounds pg_dump — the watchdog tick awaits this check; a stuck dump would otherwise freeze every subsequent healthcheck.

## Key Dependencies
- [[watchdog.ava.okf.md]] — sole caller (gateway watchdog calls `maybe_run_daily_backup` each tick)
- [[db.ava.okf.md]] — the dump target is the cluster's Postgres database

## Entry Points
- `services/backup.py:maybe_run_daily_backup()` — watchdog tick entry
- `services/backup.py:run_backup()` — actual dump + prune

## Notes
- Gateway capability only; can be disabled via `ava start --disable-service pg-backup` (through watchdog's `--disable-service` channel, same mechanism as healthchecks).
- Only protects against bad migrations / accidental deletion / DB corruption, **not against host disk failure** — offsite backup (R2/GCS) is reserved as an upload step between dump and prune, see `future/infra/pg-backup.md`.
- Restore: `pg_restore --clean --if-exists -d "<db_url>" <dump-file>`.
