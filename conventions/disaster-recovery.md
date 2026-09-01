# Disaster recovery

This matrix states the current recovery promise. A backup is not recovery
proof: the named exercise must complete successfully before its path is treated
as usable. Never restore an artifact into a live database.

| Asset | Backup or recovery media | RPO / RTO declaration | Exercise frequency |
| --- | --- | --- | --- |
| Data: Postgres, including checkpoints and conversation history | Encrypted daily local `pg_dump` artifacts in `$AVA_HOME/backups/db/`; optional immutable encrypted remote logical copy | RPO: at most one daily backup window. RTO: one maintenance window; the production-sized isolated restore measured 136.6 seconds on 2026-08-27, not a guaranteed duration. | Weekly isolated local restore after the Sunday 03:00 cluster-time dump. |
| Data: physical PITR, only after its activation gates are enabled | Immutable remote WAL and base-candidate objects, plus local ACKs and manifests | No production RPO/RTO is declared while PITR remains disabled. After activation, its recovery promise is established by a generation-pinned `prove_candidate`, not by object presence alone. | Monthly on the first day of the cluster month at 06:00, when an unprotected candidate is available. |
| Configuration: cluster `.env`, identity, and host wiring | Surviving gateway or runner `.env` secret escrow plus source-controlled installation inputs | No automated configuration-backup RPO/RTO is declared. Rebuilds require a surviving secret and an operator-led install/enrolment procedure. | Verify secret escrow during every recovery exercise and cluster-change review. |
| Code | The merged Git history and the checkout installed for the cluster | RPO: merged commits. RTO: checkout, dependency sync, and the normal cluster-update path; no wall-clock SLO is declared. | Every cluster update proves the deployed revision can start. |
| Agent state | Durable conversations and checkpoint state are in the Postgres artifacts above; live processes and in-flight turns are ephemeral | Durable state inherits the database RPO/RTO. Process state has no backup and resumes only from a completed checkpoint after restart. | The weekly logical restore verifies a checkpoint-reader sample; the PITR proof verifies a generation-pinned candidate when enabled. |

## Automated proofs and alerts

The gateway-owned `pg-backup` scheduler runs the weekly logical drill only after
a successful daily dump. It writes an owner-only success marker, so a previous
success does not suppress a later week's proof. The disabled-by-default PITR
base-candidate scheduler runs the monthly isolated proof only when
`AVA_PITR_RESTORE_PROOF_ENABLED` is enabled and a pending candidate exists.

Either failure emits the typed `recovery_drill_failed` telemetry event with the
affected drill name. Grafana alerts immediately on that event's one-hour
window. The retention planner remains viewer-only and dry-run-only: it emits
the backend-scoped remote object count and byte gauges but has no delete path.
A non-paging warning is raised if the remote byte footprint remains more than
25% above its week-ago value for one hour; a new inventory has no seven-day
baseline and stays quiet.

## Deployment boundary

This repository change prepares the schedules and observability only.
`AVA_PITR_RETENTION_PLANNER_ENABLED` and every physical-PITR gate remain false
by default, and no remote retention action is enabled. Deployment and any
enablement are coordinated by 1818: confirm the owner-only converge repair,
viewer credentials, storage baseline, Grafana rules, and a successful manual
proof before changing an activation flag.

## Restore procedure

For the isolated logical restore commands and acceptance checks, follow
[`db-restore.md`](../.agents/skills/operating-ava-cluster/references/db-restore.md).
For failed migration rollback recovery, follow
[`down-failure-drill.md`](down-failure-drill.md).
