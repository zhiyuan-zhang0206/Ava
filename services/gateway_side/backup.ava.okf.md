---
type: doc
title: PG-Backup — Daily Local Postgres Backup
description: A daily local pg_dump driven by the watchdog tick — on the gateway host, each day at the first tick after BACKUP_HOUR in cluster time, a custom-format full-database dump is made to $AVA_HOME/backups/db/ under a UTC-stamped name, keeping the newest BACKUP_KEEP copies. Protects against bad migrations / accidental deletion / DB corruption, not against disk failure.
tags: []
---

# PG-Backup — Daily Local Postgres Backup

## What is it
A daily local Postgres backup that piggybacks on the watchdog tick — not an independent daemon, but `maybe_run_daily_backup()` as a pseudo healthcheck (named `pg-backup`, no ServiceSpec) manually appended to the tick tail by the gateway watchdog's `_checks_for_capability`. Called once per tick, it only actually runs after `BACKUP_HOUR` (3 AM **cluster** time — `AVA_TIMEZONE`, cluster-pinned) today and if no dump has been done today yet; a host down at 03:00 will catch up on the next tick, rather than skipping the day. The day boundary is the cluster's, not the host's: reading the host's OS timezone made a machine that moved between timezones see its newest dump as "in the future" and skip backups silently.

## Core Responsibilities
- **Daily dump**: `pg_dump --format=custom` full database, including the LangGraph checkpoint tables that hold conversation history, then compresses and encrypts it to `$AVA_HOME/backups/db/<dbname>-YYYYMMDDTHHMMSSZ.dump.gz.enc` — under path-only cluster identity, `$AVA_HOME` itself already uniquely locates the cluster, so dump directories no longer need a cluster token (historical dumps under old `backups/<cluster-name>/` are kept in place, not migrated).
- **UTC-stamped names**: the stamp is UTC with an explicit `Z`, so prune's oldest-first ordering is a total order over real instants. Legacy `<dbname>-YYYYMMDD-HHMMSS.dump` names (host wall clock, no offset) stay managed and are read in cluster time, so a week of pre-cutover dumps still prunes out instead of stranding.
- **Atomic write**: writes private plaintext and encrypted `.partial` files first, then publishes the encrypted artifact only after both commands succeed — half-finished dumps won't be mistaken for complete backups by due/prune logic or manual recovery.
- **Keep a week**: only manages dumps matching this module's naming convention (`_NAME_RE`); manually placed dumps in the same directory are never touched; deletes those beyond the newest `BACKUP_KEEP=7`.
- **Timeout guard**: `_DUMP_TIMEOUT_S=60min` bounds pg_dump — the watchdog tick awaits this check; a stuck dump would otherwise freeze every subsequent healthcheck.

## Key Dependencies
- [[watchdog.ava.okf.md]] — sole caller (gateway watchdog calls `maybe_run_daily_backup` each tick)
- [[db.ava.okf.md]] — the dump target is the cluster's Postgres database

## Entry Points
- `services/backup.py:maybe_run_daily_backup()` — watchdog tick entry
- `services/backup.py:run_backup()` — actual dump + prune

## Notes
- Gateway capability only; can be disabled via `ava start --disable-service pg-backup` (through watchdog's `--disable-service` channel, same mechanism as healthchecks).
- Protects against bad migrations / accidental deletion / DB corruption and makes a best-effort encrypted Google Drive copy before pruning; an unavailable Drive folder leaves the local artifact intact. Object storage remains a future alternative, see `future/infra/pg-backup.md`.
- Restore: follow `.agents/skills/operating-ava-cluster/references/db-restore.md` to decrypt before `pg_restore --clean --if-exists`.
