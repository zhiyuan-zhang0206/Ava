---
type: doc
title: R1 Liveness — Registry × Lease
description: The registry×lease frame for every managed object (cluster orchestration / agent / updater), and the single alive predicate.
tags:
- okf-design
- r1
---

# R1 Liveness — Registry × Lease

## Liveness: registry × lease, one mechanism, many objects

Two questions frame every managed object: **registry** answers "should it exist?" (persistent row: who, what type, what schedule, current state); **lease** answers "is it alive?" (periodic renewal, expiry = dead).

| Object | Registry | Lease | Renewed by | Observers |
|---|---|---|---|---|
| Agent process | `agents_meta` row (exists) | `agents_meta.lease_expires_at` (new column) | the agent itself (light timer — idle renews too) | quiesce, reaper, frontend, heartbeat guard |

The frame's cluster-orchestration and updater rows are retired with the in-place updater: no cluster deploy lease or updater lease exists ([decision](../../../docs/decisions/2026-09-30-remove-deployment-lease.md)). A watcher (`ava.watcher.at/cron/launch`) deliberately sits OUTSIDE this frame: it is a plain `ava.shell.sessions` session with a shell TTL and nothing else — no registry row, no "should it exist?" record, no rebuild
(docs/decisions/2026-09-27-watchers-are-never-restarted.md). Its TTL reaper reclaim
is the same interruption notice any other reclaimed shell gets.

**Single predicate**: `alive := status ∈ {running, idling} ∧ lease unexpired` — defined once, imported everywhere. `running + lease expired = zombie` → reaper collects.


Parent: [[okf/design/r1-state-liveness/r1-state-liveness.ava.okf.md|R1 state & liveness design]].
