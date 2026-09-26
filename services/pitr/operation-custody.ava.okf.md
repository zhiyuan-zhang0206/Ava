---
type: doc
title: PITR Operation Custody
description: How logical dumps, logical restore drills, base candidates, restore proofs and operator drills run as one directly owned worker process group plus the PostgreSQL families that leave it, when results are accepted, and how failed or cancelled operations are quarantined or block their kind until retirement.
tags: []
---

# PITR operation custody

Every logical dump, logical restore drill, base candidate, restore proof and
operator drill runs as one operation of its **kind**
(`services/pitr/operation_custody.py:OperationKind`): a private control root,
its own quarantine root and a sanitizer. `services/pitr/worker_process.py:run_operation`
launches a fixed worker module through `ExecProcessDomain.launch_posix` in a
new session and retains the unreaped direct child. A bootstrap pins the
controller's own code root; the live database URL reaches restore workers on
stdin, never in a retained file. Trusted tools and each foreground postmaster are ordinary
members of that group. PostgreSQL's own children are not: every one of them
(checkpointer, walwriter, the startup process, each backend) calls `setsid` at
birth, so the group proves nothing about them.

Each kind quarantines into `quarantine/<kind>/` beside its control root.

| Kind | Control root | Sanitizer |
| --- | --- | --- |
| `logical-dump` | `backups/operations/dump/` | keeps only complete `.dump.enc` |
| `logical-restore-drill` | `backups/operations/restore-drill/` | removes the decrypted scratch, reaps the restored throwaway cluster |
| `base-candidate` | `physical-backup/base-control/` | removes `.partial` PGDATA; keeps a resumable `.ready` unless its worker rejected it |
| `restore-proof` | `physical-backup/restore-control/` | removes sandbox, WAL and ciphertext; keeps logs and owner |
| `pitr-drill` | `physical-backup/drill-control/` | none (evidence stays in the operator's scratch) |

**Closure** is proven only when three things hold:

- the worker's group is empty (confirmed SIGKILL);
- every PostgreSQL family the worker started is gone;
- the leader is reaped.

The worker receipts each postmaster it starts
(`shared/pg_foreground.py:start_foreground_postgres`) into its controls. The
data directory is receipted before launch, and the native birth right after.
The controller then closes in this order (`operation_custody.close_operation`):

1. It records the worker's tree and each receipted postmaster still in the
   pinned group, with every descendant, by exact native birth while
   parentage holds. The births go to `family.json`.
2. It stops each such postmaster cleanly with SIGQUIT (`pg_ctl stop -m
   immediate`), which exits only after reaping its children.
3. It closes the group.
4. It kills any recorded survivor by its exact birth.
5. It requires one observation with no recorded birth alive and no process
   working inside a receipted data directory (every PostgreSQL process works
   there for its whole life). Anything unresolved blocks.

## Outcomes

- **Accepted**: closure is proven, the worker exited 0 and its result is
  valid. The caller's commit (`CompletedOperation.commit`) then runs off the
  event loop, and the controls retire by atomic rename. The commit keeps the
  kind lock that `run_operation` took, so another controller's admission never
  reads a committing operation as a stopped one.
- **Deferred**: a busy backup lock or missing space, raised before any
  evidence exists, is `{"deferred", "detail"}`; the controls retire without an
  alert (`OperationDeferred`; a backup-lock deferral stays `LockTimeoutError`).
  A kind lock held elsewhere raises `OperationBusyError`, which the schedulers
  log as a deferral, never as a failed proof.
- **Quarantined**: every other outcome whose closure the controller proved.
  This includes stop, drain, timeout and a launch that never produced a
  process.
  - The sanitizer removes plaintext database material and moves the worker's
    own receipts into the controls. A receipt it cannot read is claimed by
    nobody: it is left in place and listed in `unclaimed-receipts.txt`.
  - The controls move under the kind's quarantine root with `closure.json`
    and `failure.txt`. `failure.txt` records the original failure even when a
    stop arrives during the grace or the close.
  - The next operation proceeds.
- **Blocked**: closure is unproven. Causes include a signal that was refused,
  a member that never exits, a birth that could not be closed, and a family
  member or data-directory holder that cannot be proven gone.
  - The controller keeps the actual unreaped leader for its process lifetime,
    re-attempts the full closure at the kind's next admission, and writes
    `unresolved.json`. The kind refuses new work, as it does after a
    controller died before proving closure.
  - A proven closure whose quarantine keeps failing is recorded as
    `quarantine-failed.json`. Status shows it blocked, and admission and
    retirement retry it.

Custody steps (launch, closure, commit, quarantine) run to completion in a
thread; a stop arriving meanwhile waits for their outcome, then propagates.
Cancellation first sends SIGTERM for the kind's grace (3 s; 45 s for an
operator drill, to stop its sandbox and write evidence), then runs the closure
above. An `ExecDomainBirthError` closes the pinned group at once. Admission
finishes a quarantine or retirement its controller had proven but not
completed. A failing progress sink (a closed operator pipe) is dropped.

A base candidate whose own verification or loading fails (corruption,
missing or contradicting facts, an unusable plan) is marked `rejected` in its
owner receipt, and quarantine discards the tree instead of resuming it every
retry. A stop keeps it resumable.

## Retirement

`ava pitr operations status` lists blocked kinds and each kind's quarantine.

`ava pitr operations retire` re-proves closure of each blocked operation and
`--confirm` quarantines it. A proof is one of:

- a later controller closure;
- a reboot;
- an empty process group;
- a new process at the worker's PID born after the launch record (this also
  covers a worker whose birth was never recorded).

A proof also needs the same family proof as the controller, without
signalling: every birth in `family.json` and every receipted postmaster dead,
and no process inside a receipted data directory.

Refusals are typed (`Refusal`); evidence that cannot be verified refuses as
`unverifiable` rather than crashing. Receipts, saved PGIDs and deadlines never
adopt or signal a group.

`ava pitr operations discard-candidate CHAIN [--confirm]` removes one
unfinished weekly `.ready` capture. Such a capture can refuse activation's
forced candidate, and activation keeps the schedule from resuming it. The verb
runs under the base-candidate kind lock, and refuses a blocked kind, a
committed chain, or one an unsettled owner receipt still claims.

## Quarantine and alerts

Each kind's quarantine keeps its newest entry, then at most ten entries and
8 GiB, pruned under the kind's own lock: one kind's stranded dump never evicts
another kind's evidence. These controls clean up trusted tools
and PostgreSQL; they are not a containment fence against code that escapes
both the group and its recorded parentage.

`backup_operation_custody` telemetry alerts on quarantine (warning) and
blocking (error) per kind.

## Sandbox identity and credentials

Sandbox birth, PGID and SID are captured before PID-file readiness and must
equal the worker's group and session; live PostgreSQL proof compares native
identity and exact SQL/data evidence. The restore worker runs under
`restricted_process_env()`, never loads Settings, and is bounded by the
credentials it receives: viewer-only for gcs and oss, read-write for cos and
baidu.

[[services/pitr/pitr.ava.okf.md|Physical PITR]] ·
[[services/gateway_side/backup/shutdown.ava.okf.md|Backup job shutdown]]
