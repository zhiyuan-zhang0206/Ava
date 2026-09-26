---
type: doc
title: PITR Operation Custody
description: How logical dumps, logical restore drills, base candidates, restore proofs and operator drills run as one directly owned worker process group, when results are accepted, and how failed or cancelled operations are quarantined or block their kind until retirement.
tags: []
---

# PITR operation custody

Every logical dump, logical restore drill, base candidate, restore proof and
operator drill runs as one operation of its **kind**
(`services/pitr/operation_custody.py:OperationKind`): a private control root,
a quarantine root and a sanitizer. `services/pitr/worker_process.py:run_operation`
launches a fixed worker module through `ExecProcessDomain.launch_posix` in a
new session and retains the unreaped direct child. A bootstrap puts the
controller's own code root first on the worker's path and refuses any other
import origin. The live database URL reaches restore workers on stdin, never in
a retained file. Trusted tools and the sandbox postmaster are ordinary members
of that group; none owns a subgroup or calls `setsid`.

| Kind | Control root | Quarantine | Sanitizer |
| --- | --- | --- | --- |
| `logical-dump` | `backups/operations/dump/` | `backups/quarantine/` | keeps only complete `.dump.enc` |
| `logical-restore-drill` | `backups/operations/restore-drill/` | `backups/quarantine/` | removes the decrypted scratch |
| `base-candidate` | `physical-backup/base-control/` | `physical-backup/quarantine/` | removes `.partial` PGDATA, keeps a resumable `.ready` |
| `restore-proof` | `physical-backup/restore-control/` | `physical-backup/quarantine/` | removes sandbox, WAL and ciphertext; keeps logs and owner |
| `pitr-drill` | `physical-backup/drill-control/` | `physical-backup/quarantine/` | none (evidence stays in the operator's scratch) |

- **Accepted**: confirmed group closure, exit 0, a valid result, then the
  caller's commit (`CompletedOperation.commit`) off the event loop; the
  controls retire by atomic rename.
- **Deferred**: a busy backup lock or missing space, raised before any
  evidence exists, is `{"deferred", "detail"}`; the controls retire without an
  alert (`OperationDeferred`; a lock deferral stays `LockTimeoutError`).
- **Quarantined**: every other outcome whose closure the controller proved,
  including stop, drain, timeout and a launch that never produced a process.
  The sanitizer removes plaintext database material and moves the worker's
  receipts into the controls, which move under the quarantine root with
  `closure.json` and `failure.txt`. The next operation proceeds.
- **Blocked**: closure is unproven (a signal refused, a member that never
  exits, a birth that could not be closed). The controller keeps the actual
  unreaped leader for its process lifetime, re-attempts closure at the kind's
  next admission, and writes `unresolved.json`; the kind refuses new work.
  A controller that died before proving closure leaves the same block.

Custody steps (launch, closure, commit, quarantine) run to completion in a
thread; a stop that arrives meanwhile waits for their real outcome and then
propagates as the stop, never as an ordinary error. Cancellation first sends
SIGTERM for the kind's grace (3 s; 45 s for an operator drill, so it can stop
its sandbox and write evidence), then closes the group with confirmed SIGKILL.
An `ExecDomainBirthError` closes the pinned group at once: the worker never
runs its job unowned. The kind's admission finishes a quarantine or a
retirement that its controller had proven but not completed.

**Retirement**: `ava pitr operations status` lists blocked kinds and
quarantine; `ava pitr operations retire` re-proves closure of each blocked
operation (a later controller closure, a reboot, an empty process group, or a
new process born under the worker's PID) and `--confirm` quarantines it. A
still-present worker, surviving group members, or a controller that died
before recording its worker refuse. Receipts, saved PGIDs and deadlines never
adopt or signal a group. Quarantine keeps its newest entry, then at most ten
entries and 8 GiB per root. These controls clean up trusted tools; they are
not a containment fence against code that creates an independent session.

`backup_operation_custody` telemetry alerts on quarantine (warning) and
blocking (error) per kind.

Sandbox birth, PGID and SID are captured before PID-file readiness and must
equal the worker's group and session. Live PostgreSQL proof compares native
identity and exact SQL/data evidence. The restore worker runs under
`restricted_process_env()` and never loads Settings; its boundary is the
credentials it receives: viewer-only for gcs and oss, while cos and baidu hand
it their read-write credential files.

[[services/pitr/pitr.ava.okf.md|Physical PITR]] ·
[[services/gateway_side/backup/shutdown.ava.okf.md|Backup job shutdown]]
