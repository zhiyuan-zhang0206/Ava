---
type: doc
title: Backup Job Shutdown
description: Ownership and completion of scheduled logical-backup and restore jobs.
tags: []
---

# Backup job shutdown

`services/backup_scheduler/daemon.py` passes scheduled dumps and logical restore
drills to `services/backup_scheduler/worker.py:run_job`. It launches one fixed
worker module through `services/pitr/worker_process.py:run_operation`: a fresh
interpreter in a new session whose unreaped direct child the controller keeps.
Its request is complete before launch. Dump, encryption, off-site upload and
restore tools are ordinary children in that group, so blocking work stays out
of the scheduler's asyncio loop and executor.

Scheduler SIGTERM cancels the job. The controller sends SIGTERM to the group:
the worker converts it into `KeyboardInterrupt`, so its `finally` blocks remove
key files and decrypted scratch and stop a foreground Postgres. After a
three-second grace it closes the group with confirmed SIGKILL, then the
scheduler removes its pidfile and closes health. A result is accepted only
after group closure, a zero exit and validation; the dump is then linked into
the backup directory. Cancellation never advances scheduler success or the
weekly restore marker.

Every failed or cancelled job keeps its controls (request, logs, partial
dumps, and an upload-interrupted complete artifact) under
`$AVA_HOME/backups/operations/`. The next job refuses until an operator
retires them; no stale sweep deletes partial work. Closure doubt keeps the
unreaped worker and fails loudly. A killed scheduler loses the only direct owner:
its retained controls then need explicit retirement.

Scheduled restore drills opt into `throwaway_postgres(foreground=True)`.
`shared/pg_foreground.py` starts Postgres directly in the worker group and
verifies its data directory before readiness. The caller owns its handle before
readiness checks can fail. A stop that cannot confirm the postmaster exited
retains PGDATA and its registration. Other throwaway callers keep the default
pg_ctl behavior; orphaned throwaway directories are handled by their own sweep.
Retained backup deletion and PITR upload are independent.
