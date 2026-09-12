# Pre-rollout health baseline

Full gateway orchestration records a timestamped health baseline immediately
following the last-update record, before Phase 0 and any pause. This includes
restart-only runs; dry runs and docs-only/frontend-only fast paths skip it.
The snapshot appears in the rollout log so operators can compare the state
before an update with observations after a rollback (task #3211, S2).

The baseline contains the cluster target, last-known-good and pending-known-good
pins; the gateway roster's machine capabilities, staging flags, on-pin verdicts,
online/stopped and paused state; deployment postures; this host's stored health
failure counter, pending promotion passes and alert episode; and database size
in readable units and bytes. The header identifies the intended target and UTC
collection start. Reads are sequential observations, not a transactional snapshot.
The failure timestamp is labeled `recorded-at`: the existing writer updates it
on each failure, so it does not establish when the episode began.

Collection is read-only and best-effort. The roster uses the existing authenticated
HTTP reader with a ten-second timeout. Health state is read from existing files;
no health-probe command is run. Each source degrades independently to an explicit
unavailable reason, and missing files are distinguished from parse failures.
Missing posture rows remain `(no row)`. An outer exception guard prevents an
unexpected collection or formatting failure from aborting orchestration.

This is diagnostic evidence only. It changes no health gates, promotion rules,
rollout targets, or rollback decisions, and writes no additional durable state
outside the existing rollout log.
