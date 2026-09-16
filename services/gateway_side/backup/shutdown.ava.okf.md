---
type: doc
title: Backup Job Shutdown
description: Ownership and completion of scheduled logical-backup and restore jobs.
tags: []
---

# Backup job shutdown

`services/backup_scheduler/daemon.py` passes scheduled dumps and logical restore
drills to `services/backup_scheduler/worker.py`. A spawned worker establishes a
private process group and waits for controller adoption before starting work.
This keeps blocking dump, encryption, upload and restore operations out of the
scheduler's asyncio executor.

SIGTERM interrupts the worker's synchronous job so its finally blocks can
remove partial dumps and keys. The controller reaps stubborn groups within
seven seconds before the scheduler removes its pidfile and closes health.
Success requires a result, zero exit status and no surviving group members;
cancellation never advances scheduler success or the weekly restore marker.
A completed encrypted dump survives interruption of its optional off-site upload.

Scheduled restore drills opt into `throwaway_postgres(foreground=True)`.
`shared/pg_foreground.py` starts Postgres directly in the worker group and
verifies its data directory before readiness. The caller owns its handle before
readiness checks can fail. A stop that cannot confirm the postmaster exited
retains PGDATA and its registration. Other throwaway callers keep the default
pg_ctl behavior.

A hard kill cannot run Python cleanup, so private scratch files may remain.
The existing next-run sweeps handle interrupted partial dumps and orphaned
throwaway directories. Retained backup deletion and PITR upload are independent.
