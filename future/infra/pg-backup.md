# PG backup — off-site leg (GCS / R2)

> Status (2026-06-08): the one live item carried out of the now-archived
> agent-runner bring-up follow-ups (item #5).
>
> **Update 2026-06-09: the local leg landed.** `services/backup.py` runs a
> daily `pg_dump --format=custom` on the gateway host via the watchdog tick
> (03:00 local, `$AVA_HOME/backups/db/`, `BACKUP_KEEP=1` — retention was cut
> from 3 to 1 in #832 when daily dumps filled the Mac mini's disk) — see `.agents/skills/recover-a-cluster/references/db-restore.md`. The old R2-era `scripts/pg_backup.sh` was removed
> with it. Local dumps cover bad migrations / accidental deletes / DB
> corruption; what remains open here is the **disk-loss** scenario — and a
> one-dump local window makes the off-site leg below the *only* history.
>
> **Update 2026-08-08 (#1035): the dump excludes the LangGraph runtime tables**
> (`checkpoint_blobs` / `checkpoints` / `checkpoint_writes` — `_EXCLUDE_TABLES`
> in `services/backup.py`). checkpoint_blobs alone outgrew the dump's 30-min
> ceiling and dead-looped the backup (dump killed → partial swept → retry →
> killed; the old dump never pruned, disk stuck ~90%). The checkpoints are
> RUNTIME execution state, not the system of record — the conversation stream
> lives in the `events` table (the unified read path since the W9 cutover),
> which IS dumped; checkpoints are rebuildable from events (the reconstruction
> was proven by `scripts/preview/rebuild-checkpoints.py`, since removed with
> its hardcoded secret — re-derive from `git show bdad80caa` if ever needed).
> A restore loses in-flight graph state (pending interrupts), not history.
> The remaining DB dumps in ~1 min at ~0.7 GB; `_DUMP_TIMEOUT_S` is 60 min of
> pure headroom. The excluded tables' unbounded growth (18 GB and counting,
> no retention) is a separate open item — a checkpoint retention policy.
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

1. **Add the off-site leg.** `services/backup.py:run_backup` is structured as
   `dump -> (upload slot) -> prune`; an off-site backend (GCS bucket +
   service-account creds, or a re-minted R2 token) is an upload step between
   the dump and the prune, plus a remote lifecycle/retention rule.
2. **Run a restore drill.** Pull the latest dump → `pg_restore` into a scratch
   PG → spot-check row counts. The restore path has never been exercised; it
   is not a backup until it has been restored once.
