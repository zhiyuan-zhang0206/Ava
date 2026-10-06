---
type: doc
title: Scheduled Backup Workers
description: Private staging, bounded worker stop and validated atomic publication for scheduled logical backups and restore drills.
tags: []
---

# Scheduled backup workers

`worker_process.py` launches the fixed worker module from this checkout in an
isolated interpreter and new process group. The bootstrap checks code origins.
Secrets travel on stdin, never retained request files. Each run has a unique
0700 `.operation-*` directory, private request/result files and file-backed
stdout/stderr. Progress and large artifact commits stay off the event loop.

The kind's concurrency lock prevents overlapping controller commits. A busy
lock defers the scheduler. A zero worker exit and object result precede
`CompletedOperation.commit`: dump publication validates the name and digest,
then links the exact encrypted artifact exclusively into the backup directory.
Cross-filesystem publication verifies a private copy before linking it. A
failure never advances the scheduler/restore success marker.

Cancellation gives the known worker/group a bounded TERM grace, then KILL and
a bounded direct-child wait. Normal worker cleanup removes keys, plaintext
partials and decrypted scratch. The controller also sanitizes its private
staging on failure or after commit. Cleanup errors retain an actionable path;
they are not hidden behind a successful operation verdict.

There are no process/family receipts, all-descendant disappearance proof,
quarantine, restart adoption, held unreaped-leader registry, blocked kinds or
`ava backup operations` retirement commands. Old control directories are not
read as process authority; the next run has independent scratch. Unexpected
orphan processes require operational investigation, not an automatic census.

Restore drills use existing unique throwaway PGDATA and its owner lock.
`pg_foreground.py` retains a direct-child handle and checks the actual protocol
response names the expected data directory. Native `pg_ctl` immediate shutdown
owns PostgreSQL child cleanup. A failed stop preserves the scratch directory;
no custom family/cwd scan authorizes deleting it.

[[shutdown.ava.okf.md|Backup job shutdown]]
