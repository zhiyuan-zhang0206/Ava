# Disaster recovery

This matrix states the current recovery promise. A backup is not recovery
proof: the named exercise must complete successfully before its path is treated
as usable. Never restore an artifact into a live database.

| Asset | Backup or recovery media | RPO / RTO declaration | Exercise frequency |
| --- | --- | --- | --- |
| Data: Postgres, including checkpoints and conversation history | Encrypted daily local `pg_dump` artifacts in `$AVA_HOME/backups/db/`; optional immutable encrypted remote logical copy | RPO: at most one daily backup window. RTO: one maintenance window; the production-sized isolated restore measured 136.6 seconds on 2026-08-27, not a guaranteed duration. | Weekly isolated local restore after the Sunday 03:00 cluster-time dump. |
| Data: point-in-time recovery | When `AVA_WALG_CONFIG_FILE` is set: WAL-G ships completed WAL segments (at most 60 s apart while the database is written) and takes a daily base backup (a full one every seventh run). `ava backup walg restore` recovers the database to a time, an LSN or the end of the archive. The self-written PITR stack it replaces was deleted. | RPO: at most 5 minutes (the archive objective, alerted when exceeded). RTO target: 2 hours (a restore is bounded by it). Both cover the **database under the same home identity** only: rebuilding a lost host's identity state (`$AVA_HOME/db-authority/`, `.env`, `start-intent.json`) is not covered and has never been exercised. Without WAL-G enabled none of this applies and the row above is the only recovery point. | Weekly recovery drill inside the daily WAL-G run: restores the newest backup, recovers it to the newest archived WAL segment and reads a real conversation. |
| Configuration: cluster `.env`, identity, and host wiring | Surviving gateway or runner `.env` secret escrow plus source-controlled installation inputs | No automated configuration-backup RPO/RTO is declared. Rebuilds require a surviving secret and an operator-led install/enrolment procedure. | Verify secret escrow during every recovery exercise and cluster-change review. |
| Code | The merged Git history and the checkout installed for the cluster | RPO: merged commits. RTO: checkout, dependency sync, and the normal cluster-update path; no wall-clock SLO is declared. | Every cluster update proves the deployed revision can start. |
| Agent state | Durable conversations and checkpoint state are in the Postgres artifacts above; live processes and in-flight turns are ephemeral | Durable state inherits the database RPO/RTO. Process state has no backup and resumes only from a completed checkpoint after restart. | The weekly logical restore verifies a checkpoint-reader sample. |

## Automated proofs and alerts

The gateway-owned `pg-backup` scheduler runs the weekly logical drill only after
a successful daily dump. It writes an owner-only success marker, so a previous
success does not suppress a later week's proof.

When WAL archiving is configured, the cluster health probe also reports a Postgres
that does not carry the archive settings, a failing archiver, complete WAL waiting
for archive beyond the 300 s objective, a changed encryption key, a failed daily
backup run, a broken archived WAL chain, a daily tick that has not started within
a day, a failed weekly recovery drill and a week without a successful one
(`docs/conventions/runbook.md`, "WAL-G archiving"). The drill is the proof: a passing drill
means the newest backup and the WAL behind it restored and a conversation read back.
The other alerts say the archive and the backup run are healthy or not; they are not
a recovery proof.

A failed drill emits the typed `recovery_drill_failed` telemetry event with the
drill name. Grafana alerts immediately on that event's one-hour window. Every
backup operation also reports custody through `backup_operation_custody`: a
failed or cancelled operation with proven group closure is quarantined without
plaintext and warns while the next run proceeds; unproven closure blocks that
operation kind and alerts as an error until `ava backup operations retire`
re-proves closure (`docs/conventions/runbook.md`).

## Restore procedure

For the isolated logical restore commands and acceptance checks, follow
[`db-restore.md`](../../ava_builtins/skills/platform/ava-guide/operations/references/db-restore.md).
For recovery from the WAL-G physical backup (to a time or LSN), follow
[`walg-restore.md`](../../ava_builtins/skills/platform/ava-guide/operations/references/walg-restore.md)
and the "WAL-G archiving" section of the runbook.
There is no migration rollback (no down migrations): a bad migration is
fixed forward with a new one, or recovered from the WAL-G chain; the retired mutable-checkout updater drill is not a supported recovery
entrypoint.
