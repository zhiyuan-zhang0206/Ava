---
type: doc
title: Backup Job Shutdown
description: Bounded scheduled worker stop and private artifact cleanup.
tags: []
---

# Backup job shutdown

The scheduler sends dumps and restore drills to `services/backup/scheduler/worker.py:run_job`. A fixed
worker in an isolated interpreter and new group performs blocking tools;
requests and results use unique private staging, and secrets travel on stdin.

SIGTERM cancellation asks the worker to unwind normal cleanup. After the
bounded grace, its controller can KILL the known group and wait for the direct
child. The controller sanitizes staging and reports cleanup failures with the
private path. There is no full-family disappearance proof or durable operation
custody. No quarantine/blocked-kind/retirement workflow survives controller exit.

Results require zero exit and validation. Encrypted dump publication retains
its digest, exclusive atomic publish and permissions checks. Cancellation or
failure never advances the backup or weekly restore success marker.

Foreground disposable Postgres is a known direct child: readiness checks its
actual data directory, native `pg_ctl` performs immediate shutdown, and a
failed stop retains PGDATA and its owner lock. Existing unique scratch and
orphan directory handling remain; no custom process-family/cwd census is made.

[[services/backup/scheduler/docs/scheduled-workers.ava.okf.md|Scheduled workers]]
