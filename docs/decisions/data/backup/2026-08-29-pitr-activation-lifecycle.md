# PITR activation is an explicit durable cluster operation

## Decision

Physical backup activation is entered only through `ava cluster pitr activate`.
The command owns a versioned operation record under
`$AVA_HOME/physical-backup/activation/operation.json`; its persisted
`started_at` is the clock source across process and page refreshes. Operators do
not edit PostgreSQL configuration, signal Postgres, or start backup daemons by
hand.

The first activation delivery stops at `wal_config_pending` after proving the
disabled shadow layout and creating a verified encrypted logical `pg_dump` with
the dedicated `pitr-activation-<operation-id>` kind. Ordinary daily/pre-update
retention never prunes that exact recovery floor while the operation is active;
terminal operations keep a bounded two-snapshot recovery window. Resume and status
revalidate that it is a regular mode-0600 artifact and passes the backup
verification contract. It does not change PostgreSQL. A rollout cannot deliver
its own protection, so the first rollout remains protected only by that logical
snapshot.

Preparation holds both a per-home activation lock and the cluster update owner
across record read, snapshot side effects, and record publication. Therefore an
activation, rollback, update, or maintenance transition cannot overwrite
another operation. The shadow baseline requires all three PITR service gates
off, PostgreSQL archiving off, and an empty archive command; any half-active
manual state fails closed.

Readiness binds the running PostgreSQL process to this registered cluster's
PGDATA, port, postmaster birth time, major version, and system identifier before
and after the dump. It also hashes the installed stable shim against current
source and checks its private spool layout. Credential JSON service-account emails
must be distinct (different keys for one account are not separation), with project
and key IDs retained as evidence, and both identities must perform a non-mutating bucket
metadata read in the configured project/region. This is not a write/delete IAM
proof; the privileged continuation owns that smoke test.

The dump target is not inherited from a process-wide URL. Readiness constructs a
local direct URL from the registered Postgres port/socket and database identity,
freezes it in the operation evidence, passes it explicitly to `pg_dump`, and
rechecks the same database/system identifier after publication.

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
