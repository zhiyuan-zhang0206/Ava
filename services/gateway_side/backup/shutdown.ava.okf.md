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
key files, plaintext partials and decrypted scratch and stop a foreground
Postgres. After a three-second grace it closes the group with confirmed SIGKILL
in a thread, so a stop that lands inside the close waits for its outcome and
still stops the scheduler. A result is accepted only after group closure, a
zero exit and validation; the dump is then linked into the backup directory.
Cancellation never advances scheduler success or the weekly restore marker.

Dumps and weekly logical restore drills have separate control roots under
`$AVA_HOME/backups/operations/`, so a failed drill never stops the dumps. A
failed or cancelled job whose closure was proven is quarantined under
`$AVA_HOME/backups/quarantine/`: request, logs and failure note, plus a complete
encrypted artifact when the off-site upload was interrupted; plaintext dump
output and key files are removed even when the worker was killed. The next
scheduled run proceeds. Closure doubt keeps the unreaped worker, blocks that
kind and alerts until `ava pitr operations retire` re-proves closure; a killed
scheduler leaves the same block.

Scheduled restore drills opt into `throwaway_postgres(foreground=True)`.
`shared/pg_foreground.py` starts Postgres directly in the worker group and
verifies its data directory before readiness. The caller owns its handle before
readiness checks can fail. A stop that cannot confirm the postmaster exited
retains PGDATA and its registration. Other throwaway callers keep the default
pg_ctl behavior; orphaned throwaway directories are handled by their own sweep.
Retained backup deletion and PITR upload are independent.
