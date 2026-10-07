# PG backup — off-site leg (GCS / R2)

> Status (2026-06-08): the one live item carried out of the now-archived
> agent-runner bring-up follow-ups (item #5).
>
> **Update 2026-06-09: the local leg landed.** `services/backup/dump.py` runs a
> daily `pg_dump --format=custom` on the gateway host via the watchdog tick
> (03:00 local, `$AVA_HOME/backups/db/`, `BACKUP_KEEP=1` — retention was cut
> from 3 to 1 in #832 when daily dumps filled the Mac mini's disk) — see `.agents/skills/ava-guide/operations/references/db-restore.md`. The old R2-era `scripts/pg_backup.sh` was removed
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
> headroom. The checkpoint reaper's trim opt-in was retired on 2026-09-30
> under the never-delete ruling; its implementation remains unscheduled.
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
> Each fleet update that applies migrations writes a `<db>-<ts>.pre-update.dump.enc`
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

> **Update 2026-10-01:** the daily logical backup no longer depends on the
> physical PITR stack. The off-site leg is OSS-only
> (`services/backup/artifact/offsite.py`; the destination is the
> `AVA_BACKUP_OFFSITE_*` keys, and a home without them skips the leg with one
> INFO line). The managed-name grammar
> (`services/backup/artifact/names.py`) is `<db>-<UTC stamp>.dump.enc`: the
> `.pre-update` and `.pitr-activation-*` kinds and the pre-cutover wall-clock
> stamp have no writer and are no longer managed, so prune keeps the newest
> `backup_keep` dumps. Operation custody moved to
> `services/backup/scheduler/operation/` and `ava backup operations` is the
> custody verb. The in-process pre-activation snapshot is gone.

> **Update 2026-10-02:** the self-written physical PITR stack is deleted
> ([decision](../../../docs/decisions/data/backup/2026-10-02-delete-the-self-written-pitr-stack.md)).
> The off-site destination is configured as `AVA_BACKUP_OFFSITE_ENDPOINT`,
> `AVA_BACKUP_OFFSITE_BUCKET` and `AVA_BACKUP_OFFSITE_CREDENTIALS_FILE`
> (`ava config set`); the PITR-era keys and the backend switch are gone.

## Future work

### Physical backup

There is none today. The daily logical dump is the only recovery point, so the
recovery point objective is one backup window. The replacement under design is
WAL archiving and base backups through WAL-G, not yet built; until it lands, no
document may promise point-in-time recovery.

1. **Off-site encrypted copy — delivered.** After encryption and before local
   pruning, the gateway publishes the artifact to OSS
   (`services/backup/artifact/offsite.py`) as `ava-logical/<name>`; the
   store-verified ACK (pin_token, size, checksum) is the identity. The publish
   is if-absent and immutable; a missing/unconfigured store is skipped with one
   INFO line, and a failed publish warns without discarding the local backup,
   which stays the primary copy. The publisher has no delete verb; remote
   expiry belongs to the bucket's lifecycle policy.
2. **Restore drill — delivered.** `scripts/data_plane_ops/restore_drill.py` decrypts the
   latest managed artifact (or a supplied path), restores it into scratch
   Postgres, and validates schema, agent rows, checkpoint rows, a checkpoint
   reader sample, and a service smoke.

> **Update 2026-09-01:** the gateway-owned scheduler runs the isolated logical
> restore drill once after the Sunday 03:00 cluster-time dump. A failure emits
> a typed recovery-drill event and alert; success is recorded privately.
