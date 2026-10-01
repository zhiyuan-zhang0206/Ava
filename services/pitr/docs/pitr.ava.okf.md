---
type: doc
title: Physical PITR — WAL Archive, Base Chains, and Remote Retention
description: Default-off physical PITR — WAL archiving, base chains, restore proofs, and gated remote retention across physical and logical objects.
tags: []
---

# Physical PITR — WAL Archive, Base Chains, and Remote Retention

## What is it

The physical recovery plane: continuous WAL archiving plus weekly pg_basebackup
chains that together restore the database to any point, and the retention side
that trims both remote object pools once an operator decides to. Five boolean
gates default off; an operator opens the first three with `ava config set`.
It sits beside — and does not replace — the daily logical dumps
([[services/gateway_side/backup/docs/backup.ava.okf.md|daily backup]]), which stay
mandatory.

## Entry Points

The package root holds only process entry points: their module paths are written into service
manifests, custody records, pidfile identity checks and `postgresql.auto.conf`, so they never move.
Everything else lives in `activation/`, `base_backup/`, `restore/`, `retention/`, `wal/`
and `stores/` (role protocols, GCS adapters, and the `baidu/`, `cos/`, `oss/` backends).

- `services/pitr/archive_shim.py` — stdlib-only atomic local WAL spool entry point, reserved for a later archive-mode rollout
- `services/pitr/activation/state.py` — strict schema-v5 atomic activation record; CAS transitions persist config digests, the exact home operation/action/generation binding, WAL ACK/viewer evidence, and candidate/protected digests while preserving `started_at`; older transport schemas refuse rather than acquiring new authority
- `services/pitr/uploader_daemon.py` — disabled-by-default single-worker GCS uploader; it verifies immutable conditional creates before publishing a durable local ACK
- `services/pitr/base_scheduler_daemon.py` — weekly bases, restore proofs, retention plans, and an arm-gated deletion tick
- `services/pitr/retention/planner.py` — the default-off local dry-run planner (see *Remote retention* below)
- `services/pitr/stores/logical_dump_names.py` — the retention classifier's grammar for `ava-logical/` names, wider than the daily writer's (`services/gateway_side/backup/names.py`): it still reads the `pre-update` and `pitr-activation` kinds and pre-cutover wall-clock stamps
- `cli/commands/data_plane/pitr.py` — `ava pitr retention` inspection, status, arm, disable and run-once commands

## Gates and layers

- Physical PITR is disabled by default: converge publishes a private
  per-home spool and a source-independent, self-checked shim, while
  `AVA_PITR_ENABLED` defaults false and PostgreSQL `archive_mode` stays untouched.
  No command activates it: the activation verbs and their release
  operation were removed. Protection requires exact PostgreSQL/local ACK/viewer
  WAL evidence, an operation-scoped base candidate, and its isolated restore
  proof.
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
  socket (`base.db.pg_admin`); the worker receives that password-free conninfo
  on stdin after the controller's custody-checked session. Server
  administration (WAL switch, archiver and `pg_hba` reads, `ALTER SYSTEM`,
  `data_directory`) runs on the administrator as itself over a
  `base.db.pg_admin.connect` session bound to the home's recorded postmaster
  (`pitr_admin_session`).

## Operation custody

Base candidates, restore proofs and operator drills each run on the backup
domain's operation core ([[services/backup_scheduler/docs/operation-custody.ava.okf.md|Operation custody]]):
one directly owned worker process group; results commit only after confirmed
closure, a zero exit and validation. A failed or cancelled operation with
proven closure is quarantined without plaintext and the next one proceeds;
unproven closure blocks its kind. `ava backup operations retire` covers the
two logical backup kinds only.

## Remote retention

- `AVA_PITR_RETENTION_PLANNER_ENABLED` computes a local dry-run plan across
  physical and `ava-logical/` objects: retain the latest two protected chains
  by capture identity, pin every unprotected candidate, keep continuous ACKed
  WAL/history from the oldest retained base, and mirror the local logical
  window (seven newest dailies; the newest pre-update and two newest
  activation snapshots stay as the classifier's defaults for old names).
- Logical objects without verifiable sidecars (GCS/COS, or sidecar-less
  OSS/Baidu) use strict names plus live stat and appear as weak evidence;
  verified OSS/Baidu sidecar pairs carry stronger binding.
- Malformed, missing, forked, generation-ambiguous, grammar-external or
  changing evidence blocks eligibility on both surfaces.
- The planner does not delete. `ava pitr retention arm --digest <SHA256> --confirm`
  writes the arm flag and approved digest to the home `.env`; the scheduler
  rereads them each tick. With planning on, an unblocked fresh plan whose digest
  matches for two ticks can delete remote physical/logical objects via the
  bounded `retention_delete_store()` executor and delete credentials.
  `ava pitr retention run-once --confirm` also deletes through it after a fresh
  digest check.
- To verify the gate is off, `ava pitr retention status` must show
  `armed=unset/false`, `approved_digest=unset` and, when reachable, daemon
  `delete_state=disabled/dry-run`. Unreachable means file state only.
  `ava pitr retention disable --confirm`
  clears both carriers for the next tick; an in-flight pass can finish.
  Stopping the scheduler leaves its `.env` arm state intact.

[[services/gateway_side/backup/docs/backup.ava.okf.md|Daily local backup]]
