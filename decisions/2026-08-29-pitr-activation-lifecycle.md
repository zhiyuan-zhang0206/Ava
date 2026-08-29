# PITR activation is an explicit durable cluster operation

## Decision

Physical backup activation is entered only through `ava cluster pitr activate`.
The command owns a versioned operation record under
`$AVA_HOME/physical-backup/activation/operation.json`; its persisted
`started_at` is the clock source across process and page refreshes. Operators do
not edit PostgreSQL configuration, signal Postgres, or start backup daemons by
hand.

The first activation delivery stops at `wal_config_pending` after proving the
disabled shadow layout and creating a verified encrypted logical `pg_dump`. It
does not change PostgreSQL. A rollout cannot deliver its own protection, so the
first rollout remains protected only by that logical snapshot.

The privileged continuation is a separate delivery. It must add a real
least-privilege GCS write/read/delete smoke boundary, atomically configure WAL
archiving, dispatch the existing Ava cluster restart, and resume from the same
operation record. Local spool publication is not remote durability. Base
candidates remain disabled until a generation-pinned WAL ACK is observed, and
`protected=true` remains impossible until a real isolated restore proof passes.

Rollback is the same CLI lifecycle. It restores snapshotted PostgreSQL settings
once later phases can mutate them, but never deletes the logical snapshot,
remote objects, or candidate evidence.

## Rejected

- Enabling all flags in `.env`: skips readiness, restart ownership, and proof.
- Direct `ALTER SYSTEM`, `pg_ctl`, or signals from a runbook: creates a second
  lifecycle with no durable resume or rollback identity.
- Treating `archive_command` success as protection: it proves only the local
  spool copy, not an exact remote generation or a restorable chain.
