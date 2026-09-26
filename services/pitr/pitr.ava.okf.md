---
type: doc
title: Physical PITR — WAL Archive, Base Chains, and Remote Retention
description: The disabled-by-default physical point-in-time-recovery plane beside the daily logical dumps — per-home WAL spool and shim, the gated GCS uploader, the weekly base-candidate and restore-proof schedulers, and the dry-run retention planner over both deletion surfaces (the PITR prefix and the `ava-logical/` dump pool) until an operator arms the deletion role.
tags: []
---

# Physical PITR — WAL Archive, Base Chains, and Remote Retention

## What is it

The physical recovery plane: continuous WAL archiving plus weekly pg_basebackup
chains that together restore the database to any point, and the retention side
that trims both remote object pools once an operator decides to. Every layer is
disabled by default and gated separately; enabling one never enables the next.
It sits beside — and does not replace — the daily logical dumps
([[services/gateway_side/backup/backup.ava.okf.md|daily backup]]), which stay
mandatory.

## Entry Points

- `services/pitr/archive_shim.py` — stdlib-only atomic local WAL spool entry point, reserved for a later archive-mode rollout
- `services/pitr/activation_state.py` — strict schema-v5 atomic activation record; CAS transitions persist config digests, the exact home operation/action/generation binding, WAL ACK/viewer evidence, and candidate/protected digests while preserving `started_at`; older transport schemas refuse rather than acquiring new authority
- `services/gateway_side/backup/snapshot.py` — creates and verifies activation's
  encrypted logical recovery floor independently of updater orchestration;
  activation reuses a previously recorded artifact only after re-verifying it
- `services/pitr/uploader_daemon.py` — disabled-by-default single-worker GCS uploader; it verifies immutable conditional creates before publishing a durable local ACK
- `services/pitr/base_scheduler_daemon.py` — separately gated weekly scheduler for physical base candidates and generation-pinned restore proofs; both gates default off and it never deletes remote data
- `services/pitr/retention_planner.py` — the default-off local dry-run planner (see *Remote retention* below)
- `services/pitr/logical_dump_names.py` — the shared managed-name grammar the daily backup writer and the retention classifier both parse, so a name the writer emits is exactly a name the planner may ever delete
- `cli/commands/pitr.py` — read-only `ava pitr retention inspect` view of the latest durable local plan, with per-surface (physical/logical) counts and the weak-evidence count

## Gates and layers

- Physical PITR is disabled by default: converge publishes a private
  per-home spool and a source-independent, self-checked shim, while
  `AVA_PITR_ENABLED` defaults false and PostgreSQL `archive_mode` stays untouched.
  `ava cluster pitr activate` first validates that shadow posture and creates a
  verified encrypted logical snapshot under the reserved
  [home operation authority](../../cli/release_transition/release_transition.ava.okf.md),
  then closes and starts root plus the native data plane using one captured image. Protection requires exact PostgreSQL/local ACK/
  viewer WAL evidence, an operation-scoped base candidate, and its isolated
  restore proof. Explicit rollback restores frozen settings under that same authority
  and never deletes backup data.
- A local archived segment is not a remote ACK. When explicitly enabled, the
  GCS uploader encrypts each spooled segment, conditionally creates one immutable
  object, verifies its generation/CRC32C/metadata, and only then fsyncs an ACK
  before removing local staging and spool files. Daily logical dumps and
  activation's initial logical floor remain independent.
- `AVA_PITR_BASE_BACKUP_ENABLED` is a second, default-off gate: enabling WAL
  upload alone cannot accidentally start a multi-GiB weekly base. A candidate
  is born as one plain `pg_basebackup -Fp -X none` tree under the shared backup
  lock, locally checked with `pg_verifybackup`, then uploaded outside the lock
  as a deterministic canonical tar → zstd → AEAD stream. The stream is read
  twice (identity preflight then upload) but no complete tar or ciphertext is
  written locally.
- A third default-off restore-proof gate downloads each exact object generation
  once into owned quarantine, authenticates locally, replays through an exact
  WAL allowlist in a sibling PostgreSQL instance, verifies stable fingerprints,
  and only then publishes a new immutable `protected=true` manifest. It never
  edits the candidate, touches live PGDATA, selects a latest object, or deletes
  remote data.
- The second gate also requires an explicit local least-privilege replication
  URL; the ordinary cluster owner remains `NOSUPERUSER` without `REPLICATION`.
- Database dials never use a write-generation login. Capture facts, the
  restore worker's live probes and the operator drill's live counts read as
  the administrator acting as the schema owner over the home's owner-only
  socket (`shared.pg_admin`); the worker receives that password-free conninfo
  on stdin after the controller's custody-checked session. Server
  administration (WAL switch, archiver and `pg_hba` reads, `ALTER SYSTEM`,
  `data_directory`) runs on the administrator as itself over a
  `shared.pg_admin.connect` session bound to the home's recorded postmaster
  (`pitr_admin_session`).

## Operation custody

Base candidates, restore proofs and operator drills (and the logical backup
jobs) each run as one directly owned worker process group; results commit only
after confirmed closure, a zero exit and validation. A failed or cancelled
operation with proven closure is quarantined without plaintext and the next
one proceeds; unproven closure blocks its kind until `ava pitr operations
retire`: [[services/pitr/operation-custody.ava.okf.md|Operation custody]].

## Remote retention (dry run)

- The optional `AVA_PITR_RETENTION_PLANNER_ENABLED` slice computes only a local
  dry-run plan over both deletion surfaces: the latest two protected chains by
  capture identity, every unprotected candidate pinned, continuous ACKed
  WAL/history from the oldest retained base, and the `ava-logical/` dump pool
  under the window mirroring the daily backup's local prune (the newest seven
  dailies plus the newest pre-update snapshot plus the newest two activation
  snapshots, and the in-flight activation operation's pinned snapshot).
- Logical objects without a verifiable sidecar binding (GCS/COS by nature, or a
  sidecar-less straggler on OSS/Baidu) keep strict-naming-plus-live-stat
  evidence and are named in the plan's weak-evidence list; a verified sidecar
  pair carries the full-strength binding on OSS/Baidu.
- Any malformed, missing, forked, generation-ambiguous, grammar-external or
  concurrently changing evidence blocks all eligibility — on both surfaces.
- It has no delete API, credential or production-enable path; remote execution
  remains future work until an operator arms the deletion role.

[[services/gateway_side/backup/backup.ava.okf.md|Daily local backup]]
