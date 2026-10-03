# Recovery drill observability and cadence

## Decision

Converge repairs existing files below the private `logs`, `workspaces`, and
`memory` trees to owner-only modes. It rejects symlinks rather than traversing
an attacker-controlled target. This makes a subsequent deployment repair
legacy permission drift without broadening the paths it owns.

The retention planner publishes only its viewer-read inventory footprint:
backend, object count, and bytes. These are absolute Prometheus gauges. The
planner remains dry-run-only and the storage contract retains no delete
operation.

The daily logical-backup scheduler owns the weekly isolated logical restore
proof. The physical base-candidate scheduler owns the monthly isolated PITR
`prove_candidate` path when its existing gate is enabled. A proof failure is a
typed telemetry event and an alertable operational fact; it is never converted
into a successful backup or protected candidate.

## Consequences

- A weekly logical proof runs once after the Sunday 03:00 cluster-time dump.
- A monthly PITR proof runs once after the first-day 06:00 cluster-time window
  when an unprotected candidate is present.
- Remote byte growth above 25% week over week is visible as a warning after a
  full baseline week; it does not enforce retention.
- All physical-PITR and retention-planner flags remain disabled by default.
  Deployment and enablement stay with 1818.
