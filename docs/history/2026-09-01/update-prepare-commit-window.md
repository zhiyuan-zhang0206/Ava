# Update prepare and commit window

## Decision

Cluster update now separates its non-disruptive prepare work from the
stop-the-world commit window. Prepare resolves the pinned target, fetches
eligible runners, captures the gateway recovery tuple and verified local dump,
checks runner reachability, builds a detached target worktree, warms its uv
environment, constructs target Settings from the current environment image,
and imports the daemon entry modules selected by the target capability roster.

The local gateway leg receives the prepared recovery tuple and never recreates
it after services are stopped. This keeps the recovery anchor available while
the data plane is still live and makes the commit path contain only pause,
stop, checkout/sync/start, readiness, and Phase B.

`ava cluster update --dry-run` performs only the read-only target/Phase-0 and
prepare checks. It neither writes a baseline file nor captures a snapshot,
acquires a rollout lease, changes pause posture, stops services, advances the
pin, or checks out code.

The maintenance estimate is the p95 of the ten most recent values for exactly
`stop_the_world`, `local_leg`, `readiness`, and `phase_b`, stored in
`$AVA_HOME/update-baseline.json`. A missing baseline records the planned stage
targets but returns a conservative 110-second first estimate until a real
rollout contributes measured data. Snapshot and remote publication remain
outside these commit-stage measurements.

Pre-update database backups remain local and verified during prepare. Their
offsite publication runs only after rollout finalization in a detached backup
module process, so a slow or unavailable remote store cannot delay recovery or
the maintenance window. A best-effort read-only offsite stat probe is
informational and never blocks commit.

## Rationale

The rollout's safety artifacts and target validation must complete before
service disruption, while the old gateway and data plane are available. Remote
publication does not improve the immediate rollback path because that path
uses the verified local artifact; coupling it to stop-the-world only lengthens
the outage risk.

The 2026-09-01 user ruling sets smooth-mode quiesce to five seconds and keeps
force mode at approximately ten seconds. With the other measured commit stages,
the target stop-the-world interval is approximately eight seconds.
