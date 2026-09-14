---
type: doc
title: R1 Liveness — Registry × Lease
description: The registry×lease frame for every managed object (cluster orchestration / agent / updater / watcher), watcher rebuild on boot reconcile, and the single alive predicate.
tags:
- okf-design
- r1
---

# R1 Liveness — Registry × Lease

## Liveness: registry × lease, one mechanism, many objects

Two questions frame every managed object: **registry** answers "should it exist?" (persistent row: who, what type, what schedule, current state); **lease** answers "is it alive?" (periodic renewal, expiry = dead).

| Object | Registry | Lease | Renewed by | Observers |
|---|---|---|---|---|
| Cluster orchestration | `deployment_state` row | `lease_expires_at` | orchestrator (existing) | controllers, status, recover |
| Agent process | `agents_meta` row (exists) | `agents_meta.lease_expires_at` (new column) | the agent itself (light timer — idle renews too) | quiesce, reaper, frontend, heartbeat guard |
| Updater process | `host_deploy_state` row | `host_deploy_state.updater_lease_expires_at` | the updater | stalled judgment, Phase B polling |
| **Watcher** | **`agent_watchers` table (new)** | the session's shell TTL (`agent_shell_ttls`, written as the watcher's target deadline; user ruling 2026-09-14) | — a derived deadline, not renewable; extend by re-registering | agent reconcile (rebuild before it), TTL reaper (reclaims at it) |

Today's gaps fall out of the frame: agents have a registry but no lease; watchers have a session but no registry (the #1014 class, 4th recurrence — a kill of the session left nothing knowing it should exist; sessions now survive rollouts, and the watcher registry closed the rest); updaters have neither.

**Watcher rebuild**: `ava.watcher.at/cron/launch` writes a registry row at creation; agent boot reconciles — row exists but session missing → rebuild (cron restores from its expression; missed one-shots mark `missed` + alert). The session's shell TTL IS the watcher's deadline (launch = created + timeout, cron = end, at = moment + grace, user ruling 2026-09-14): the TTL reaper reclaims the session there and marks a still-`running` row `reaped` — past the deadline nothing is rebuilt, so reclaim and reconcile share one rule.

**Single predicate**: `alive := status ∈ {running, idling} ∧ lease unexpired` — defined once, imported everywhere. `running + lease expired = zombie` → reaper collects. For a watcher, lease expiry is its own target deadline: the reclaim terminalizes the row (`reaped`), never rebuilds.


Parent: [[okf/design/r1-state-liveness/r1-state-liveness.ava.okf.md|R1 state & liveness design]].
