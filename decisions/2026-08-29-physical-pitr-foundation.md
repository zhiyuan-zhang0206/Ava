# Physical PITR uses a local archive spool with remote acknowledgement

## Context

The logical `pg_dump` backups remain the simple, portable recovery floor, but
they cannot recover changes between dumps and a local/Drive copy does not
survive every machine-loss event. Production currently generates about 26.14
GiB WAL/day from a 6.27 GiB PGDATA. The target is a five-minute RPO without
putting cloud availability on the transaction path.

Two retained weekly chains average roughly 287–306 GiB in Taiwan Standard
Storage: about USD 5.7–6.1/month plus about USD 0.3 requests; the fourteen-day
worst case is about USD 8.2, and 20% growth remains below USD 10/month. These
numbers are planning inputs, not a billing guarantee.

## Decision

PostgreSQL `archive_command` will call a source-independent shim under
`$AVA_HOME/runtime/pg-archive/`. The shim durably and atomically publishes each
completed WAL/history file to a private local spool. A separately supervised
uploader will encrypt with a physical-backup key unrelated to the cluster
secret, publish to GCS, verify the remote object, and only then record an ACK.

Local archive and remote ACK are different states. PostgreSQL may recycle a
segment after the local spool accepts it; the off-site RPO is therefore bounded
by `archive_timeout` plus uploader lag. The initial timeout is 60 seconds; busy
traffic normally fills a 16 MiB segment sooner. Migrations will eventually gate
on a named restore point being covered by a verified base chain and remote ACK.

The spool warns near 1.1 GiB unacknowledged and refuses new publications near
2.2 GiB (about one and two production hours). Refusal never deletes unacknowledged
data: PostgreSQL retains the original WAL in `pg_wal`. The real disk exposure is
therefore **spool plus pg_wal**, so the quota is a backpressure signal, not a
claim that the disk cannot fill.

Weekly full `pg_basebackup` artifacts will be verified with `pg_verifybackup`.
Remote retention keeps at least two complete chains and every WAL/history file
from the oldest retained verified base's start LSN; WAL is never pruned
independently by modification time.

## Consequences

- GCS outages do not block commits or put network SDKs in the postmaster's
  archive child, but local disk pressure becomes the urgent failure mode.
- Backup credentials and encryption keys become an independent operational
  secret with their own rotation and disaster-recovery procedure.
- Daily and migration-bearing `pg_dump` backups stay enabled in parallel.
- The rollout that installs this protection is not protected by it. PITR is not
  declared ready until a later run has a verified remote base plus continuous,
  remotely acknowledged WAL coverage.

## Alternatives considered

- **Google Drive copies only:** useful for encrypted logical dumps but no
  continuous WAL chain, remote object acknowledgement, or point-in-time RPO.
- **Synchronous GCS in `archive_command`:** strongest immediate remote ACK, but
  network failure blocks the archiver and grows `pg_wal`; credentials and a
  cloud client also enter PostgreSQL's process boundary.
- **`pg_receivewal`:** streams below segment granularity, but needs replication
  credentials/slots. Its flush acknowledgement means local receiver durability,
  not GCS durability, and a stalled slot can retain unbounded WAL. It remains a
  future option if the five-minute target proves insufficient.
