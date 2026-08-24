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
> **Update 2026-08-19: the two clocks are pinned.** `BACKUP_HOUR = 3` is read
> on the **cluster** wall clock (`AVA_TIMEZONE`), and dumps are named in UTC
> (`<db>-YYYYMMDDTHHMMSSZ.dump`). Reading the host's clock had made a machine
> that changed timezone see its newest dump as dated in the future and skip the
> daily backup silently; the local filename had no offset, so prune's
> oldest-first ordering was ambiguous across the DST fall-back hour — the hour
> in which two dumps also collided on one name. Retention is `BACKUP_KEEP = 7`
> (raised from the 1 quoted above), so the off-site leg is no longer the only
> history.

## Future work

1. **Off-site encrypted copy — delivered.** After encryption and before local
   pruning, the gateway copies the published artifact to the existing writable
   Google Drive sync folder in a cluster-scoped backup directory, verifies its
   byte count, and applies the same seven-artifact lifecycle there. The copy is
   encrypted before it reaches Drive; a missing or non-writable sync folder
   warns without discarding the local backup. Object storage remains a future
   alternative when a host cannot use Drive.
2. **Restore drill — delivered.** `scripts/restore_drill.py` decrypts the
   latest managed artifact (or a supplied path), restores it into scratch
   Postgres, and validates schema, agent rows, checkpoint rows, a checkpoint
   reader sample, and a service smoke. The operator schedules the first
   production-sized execution separately.
