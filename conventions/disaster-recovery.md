# Disaster recovery

This matrix states the current recovery promise. A backup is not recovery
proof: the named exercise must complete successfully before its path is treated
as usable. Never restore an artifact into a live database.

| Asset | Backup or recovery media | RPO / RTO declaration | Exercise frequency |
| --- | --- | --- | --- |
| Data: Postgres, including checkpoints and conversation history | Encrypted daily local `pg_dump` artifacts in `$AVA_HOME/backups/db/`; optional immutable encrypted remote logical copy | RPO: at most one daily backup window. RTO: one maintenance window; the production-sized isolated restore measured 136.6 seconds on 2026-08-27, not a guaranteed duration. | Weekly isolated local restore after the Sunday 03:00 cluster-time dump. |
| Data: point-in-time recovery | None. The self-written PITR stack was deleted and its replacement is not built yet. | No RPO/RTO is declared below the daily dump: a recovery point newer than the last published dump does not exist. | None. |
| Configuration: cluster `.env`, identity, and host wiring | Surviving gateway or runner `.env` secret escrow plus source-controlled installation inputs | No automated configuration-backup RPO/RTO is declared. Rebuilds require a surviving secret and an operator-led install/enrolment procedure. | Verify secret escrow during every recovery exercise and cluster-change review. |
| Code | The merged Git history and the checkout installed for the cluster | RPO: merged commits. RTO: checkout, dependency sync, and the normal cluster-update path; no wall-clock SLO is declared. | Every cluster update proves the deployed revision can start. |
| Agent state | Durable conversations and checkpoint state are in the Postgres artifacts above; live processes and in-flight turns are ephemeral | Durable state inherits the database RPO/RTO. Process state has no backup and resumes only from a completed checkpoint after restart. | The weekly logical restore verifies a checkpoint-reader sample. |

## Automated proofs and alerts

The gateway-owned `pg-backup` scheduler runs the weekly logical drill only after
a successful daily dump. It writes an owner-only success marker, so a previous
success does not suppress a later week's proof.

A failed drill emits the typed `recovery_drill_failed` telemetry event with the
drill name. Grafana alerts immediately on that event's one-hour window. Every
backup operation also reports custody through `backup_operation_custody`: a
failed or cancelled operation with proven group closure is quarantined without
plaintext and warns while the next run proceeds; unproven closure blocks that
operation kind and alerts as an error until `ava backup operations retire`
re-proves closure (`conventions/runbook.md`).

## Restore procedure

For the isolated logical restore commands and acceptance checks, follow
[`db-restore.md`](../.agents/skills/operating-ava-cluster/references/db-restore.md).
Migration rollback is the paired `.down.sql` applied through `rollback_to`;
the retired mutable-checkout updater drill is not a supported recovery
entrypoint.
