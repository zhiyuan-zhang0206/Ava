# Self-evolution rollback mechanism

> **Date**: 2026-06-29
> **Status**: implemented (PR in review)
> **Builds on**: `update-failure-recovery.md` (since removed), [commit-pinned-cluster.md](../future/infra/commit-pinned-cluster.md)

## Decision

Add an OS-level cron health probe + automatic rollback to last-known-good
release, closing the "running but broken" gap the update-time recovery (Layers
1-2) cannot cover.

## Context

Today's safety net covers only the update *crash* path:
- Layer 1: orchestration auto-unpause (`cluster/resume`)
- Layer 2: gateway rollback-to-last-known-good (`_recover_gateway_local`)

Both fire only during `ava update`. Outside an update, there is no health
monitoring and no rollback path. A cluster that is *running* but *broken*
(e.g. all agents crash-looping after a bad prompt change) stays broken until a
human notices.

The watchdog (`services/watchdog/daemon.py`) revives dead daemons but does not
assess *correctness* — a running process returning 500s passes the liveness
check. So the watchdog cannot detect this class of failure.

## Decision

1. **OS cron health probe** (`ava cluster health-probe`): runs periodically
   (default every 5 min via launchd/crontab), checks gateway liveness, agent
   population, crash-loop patterns, and schema health. Exits 0 (healthy) or 1
   (unhealthy).

2. **`last_known_good_sha`**: persisted in `cluster_pin`, set atomically to
   the *previous* `target_sha` on each successful backend rollout. This is the
   automatic rollback anchor.

3. **`ava cluster rollback`**: operator verb that reuses the existing recovery
   sequence (quiesce agents → rollback schema → git reset → `ava start`) but
   targets an arbitrary commit (default: `last_known_good_sha`). Works both
   manually (`--to <tag|sha>`) and automatically (cron wrapper after N
   consecutive probe failures).

4. **Cron registration on `ava start`**: the converge step registers the OS
   cron job when the cluster comes up, so the health probe is always present.

## Alternatives considered

### Extend the watchdog instead of OS cron
The watchdog runs as a user-space session. If the cluster itself is broken
(e.g. the session server crashes), the watchdog is dead too. OS cron (launchd on
macOS, crontab on Linux) is the right layer for a "cluster is alive" check
because it runs *outside* the cluster's own process tree.

### Stack of known-good SHAs
A single `last_known_good_sha` is sufficient for a single-user system where
updates happen at human timescales. If the user needs to roll back past the
last update, `--to <tag>` covers it.

### Multi-machine rollback fan-out
Deferred: single-box (gateway+agent-runner on one machine) is the common case.
Split-deployment rollback builds on the same primitives and is a follow-up.

## Impact

- New migration (0063): `last_known_good_sha` + `last_known_good_at` on `cluster_pin`
- New CLI commands: `ava cluster {health-probe,rollback,cron-register,cron-unregister}`
- `ava start` now registers the OS cron job automatically (via converge)
- Backend updates now atomically advance `last_known_good_sha` (via `advance_pin`)
