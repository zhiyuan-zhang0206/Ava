---
type: doc
title: PITR Operation Custody
description: How base candidates, restore proofs and operator drills run as one directly owned worker process group, when their results are accepted and business state committed, and what a failed, cancelled or unresolved operation retains.
tags: []
---

# PITR operation custody

Each base candidate, restore proof and operator drill runs as one operation:
`services/pitr/worker_process.py:run_operation` launches a fixed worker module
(`base_worker`, `restore_worker`) through `ExecProcessDomain.launch_posix` in a
new session, writes its request before launch and retains the unreaped direct
child. Trusted tools (`pg_basebackup`, `pg_verifybackup`, the stream producer)
and the sandbox postmaster are ordinary children in that group; none owns a
subgroup, calls `setsid`, or starts through `pg_ctl`.

- **Acceptance**: the controller confirms group closure through the retained
  leader, requires exit 0, validates the atomically published result, and
  only then commits business state: the candidate manifest
  (`commit_base_candidate`), restore scratch retirement (`retire_restore_work`)
  and protected publication, or the drill evidence digest and `pass` outcome.
  Commit verifies that staged evidence names this exact worker birth.
- **Cancellation**: stop, timeout or task cancellation sends SIGTERM to the
  group for a bounded grace, so the worker can unwind private cleanup, then
  closes the group with confirmed SIGKILL. A result is never read after an
  abort.
- **Retention**: every failure keeps the operation's request, logs, result and
  partial work under `physical-backup/{base,restore,drill}-control/` plus the
  business staging and owner receipt. A retained control directory refuses the
  next operation until an operator retires it. A lock-held deferral is the one
  clean non-success result; its controls retire.
- **Restricted restore**: `restore_worker` runs under `restricted_process_env()`
  with viewer-only store arguments and cannot import Settings or a publisher.
- **Unresolved**: closure doubt (birth capture, signal refusal, a member that
  never exits) keeps the unreaped leader and evidence and raises. Receipts,
  saved PGIDs, deadlines and empty scans never adopt or close a group.
  Controller death on plain POSIX/macOS loses the only direct owner; its
  retained controls then need explicit retirement after native evidence, not
  automatic recovery. These controls clean up trusted tools; they are not a
  containment fence against code that creates an independent session.

Sandbox birth, PGID and SID are captured before PID-file readiness and must
equal the worker's group and session. Live PostgreSQL proof compares native
identity and exact SQL/data evidence.

[[services/pitr/pitr.ava.okf.md|Physical PITR]] ·
[[services/gateway_side/backup/shutdown.ava.okf.md|Backup job shutdown]]
