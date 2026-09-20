# PG backup — off-site leg (GCS / R2)

> Status (2026-06-08): the one live item carried out of the now-archived
> agent-runner bring-up follow-ups (item #5).
>
> **Update 2026-06-09: the local leg landed.** `services/backup.py` runs a
> daily `pg_dump --format=custom` on the gateway host via the watchdog tick
> (03:00 local, `$AVA_HOME/backups/db/`, `BACKUP_KEEP=1` — retention was cut
> from 3 to 1 in #832 when daily dumps filled the Mac mini's disk) — see `.agents/skills/operating-ava-cluster/references/db-restore.md`. The old R2-era `scripts/pg_backup.sh` was removed
> with it. Local dumps cover bad migrations / accidental deletes / DB
> corruption; what remains open here is the **disk-loss** scenario — and a
> one-dump local window makes the off-site leg below the *only* history.
>
> **Update 2026-08-25 (#1553): the dump includes the LangGraph checkpoint
> tables** (`checkpoint_blobs` / `checkpoints` / `checkpoint_writes`) because
> they are the only copy of full conversation history. The Postgres `events`
> table is a frozen archive; the live event stream is in Loki. Each completed
> dump is encrypted before publication, and the restore drill validates a
> decrypted artifact in an isolated Postgres instance. The measured full dump
> is about 849 MiB and 6.3 minutes; `_DUMP_TIMEOUT_S` remains 60 minutes of
> headroom. Checkpoint retention is owned separately by the checkpoint reaper.
>
> **Update 2026-09-21:** the `events` table's frozen archive was dropped with
> the archive cleanup (task #1281/#1823) — event history reads from Loki, and
> the emitter's JSONL mirror is the local backfill source.
>
> **Update 2026-08-19: the two clocks are pinned.** `BACKUP_HOUR = 3` is read
> on the **cluster** wall clock (`AVA_TIMEZONE`), and dumps are named in UTC
> (`<db>-YYYYMMDDTHHMMSSZ.dump`). Reading the host's clock had made a machine
> that changed timezone see its newest dump as dated in the future and skip the
> daily backup silently; the local filename had no offset, so prune's
> oldest-first ordering was ambiguous across the DST fall-back hour — the hour
> in which two dumps also collided on one name. Retention is `BACKUP_KEEP = 7`
> (raised from the 1 quoted above), so the off-site leg is no longer the only
> history.
>
> **Update 2026-08-26 (#3347): pre-update snapshots get their own retention slot.**
> **Update 2026-09-14:** enabled PITR now replaces the migration-bearing
> update's full export with a drilled base plus a freshly verified WAL recovery
> point. The logical snapshot slot below remains for PITR-disabled clusters;
> daily logical backups and activation's initial recovery floor are unchanged.
> Each `ava cluster update` that applies migrations writes a `<db>-<ts>.pre-update.dump.enc`
> snapshot into the same pool before stopping anything (pre-2026-08-27 artifacts
> carry `.dump.gz.enc`; both stay managed). Prune keeps the newest
> `BACKUP_KEEP = 7` **daily** dumps plus the newest one pre-update snapshot — an
> update never silently consumes a daily-dump slot, and the newest snapshot (the
> most recent full dump before a migration) is always retained. Local and Drive
> share the same prune.
>
> **Update 2026-08-25:** the gateway-owned pg-backup scheduler daemon now owns
> the local schedule. The watchdog probes and restarts that daemon but never
> runs `pg_dump` in its supervision round.
> **Update 2026-09-17:** the schedule and retention are cluster config —
> `services.backup_hour` / `services.backup_keep` (defaults 3 / 7; task #3696).
> Earlier bullets quote the constants' old home.
>
> **Update 2026-09-16:** scheduled dumps and restore drills use owned worker
> processes so stopping the scheduler also stops its synchronous work. Restore
> Postgres stays in that worker group; only verified completion advances success.
> **Update 2026-09-16 (2):** the gateway update's stop leg pre-checks the
> scheduler's progress and any detached off-site publish; a stop refuses
> (nothing stopped) while either is in flight (task #3661).

> **Update 2026-08-30 (audit P0-2):** the off-site leg no longer uses the
> Google Drive sync folder — it publishes through the shared BlobStore store
> group (`services.pitr.store_factory`), so GCS and Baidu Netdisk are the
> shared adapters and the Drive copy is gone. Remote objects are append-only
> except policy-owned retention deletions (off by default; the publish
> contract has no delete verb and the deletion role must be explicitly armed);
> a shared remote-retention planner is the follow-up.

## Future work

### Physical PITR delivery

The foundation is source-controlled in `services/pitr/`: per-`AVA_HOME` private
layout, a stable stdlib-only archive shim, a disabled-by-default GCS uploader,
and a health-state contract that never equates local archive with remote ACK.
The uploader uses the official Google Cloud Storage SDK rather than a `gcloud`
subprocess (host/tool coupling) or handwritten REST (duplicated auth, resumable
upload, checksum, and conditional retry machinery). It performs immutable
generation-zero creates, verifies CRC32C/generation/metadata, and fsyncs a local
ACK before deleting local staging or spool data. It never deletes remote objects.
Activation remains default-off and operator-owned: it journals the environment and
`ALTER SYSTEM` changes, continues through the existing whole-cluster restart, proves
an exact writer-smoke WAL through a durable ACK and independent viewer, then forces and
restores one operation-scoped base chain. Existing verified pre-update `pg_dump`
remains mandatory and is never pruned by physical-backup retention.

1. **Off-site encrypted copy — delivered.** After encryption and before local
   pruning, the gateway publishes the artifact through the shared backup store
   contract (`services.pitr.store_factory` -> `RestartableStreamingObjectStore
   .put_base_if_absent`, the same backend switch as the physical PITR plane) as
   `ava-logical/<name>`; the store-verified ACK (pin_token, size, checksum) is
   the identity. The publish is if-absent and immutable; a missing/unconfigured
   store or a failed publish warns without discarding the local backup, which
   stays the primary copy. Remote objects are append-only except policy-owned
   retention deletions (off by default): the store contract deliberately has
   no delete verb, and the retention planner is dry-run until an operator arms
   the deletion role. The planner now covers this pool too: the `ava-logical/`
   objects are decided under the retention window mirroring the local prune
   (newest seven dailies + one pre-update + two activation snapshots + the
   in-flight activation pin), with objects that carry no verifiable sidecar
   binding labeled weak-evidence in the plan — see the storage-abstraction
   effort's retention planner.
2. **Restore drill — delivered.** `scripts/restore_drill.py` decrypts the
   latest managed artifact (or a supplied path), restores it into scratch
   Postgres, and validates schema, agent rows, checkpoint rows, a checkpoint
   reader sample, and a service smoke.

> **Update 2026-09-01:** the gateway-owned scheduler runs the isolated logical
> restore drill once after the Sunday 03:00 cluster-time dump. A failure emits
> a typed recovery-drill event and alert; success is recorded privately. The
> physical-PITR monthly proof and the retention planner remain separately
> gated, dry-run-safe work owned for deployment by 1818.
>
> **Update 2026-09-16 (P2):** the retention planner's logical-surface half
> landed: the `ava-logical/` pool is inventoried (per-backend, viewer
> credentials), classified by the shared name grammar, and planned under the
> same dry-run/arm discipline as the physical chains — sidecar-paired objects
> carry the full-strength binding on OSS/Baidu, sidecar-less objects are
> handled strict-naming-plus-stat and marked weak-evidence. Still dry-run
> until the operator arms the deletion role.
