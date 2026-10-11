---
type: doc
title: Delivery Watchdog — wake dispatcher + stale-pending alerter
description: "Gateway wake dispatch, stale-pending alerts and stale-inbound dead-letter sweeps. No automatic resurrection or stalled-turn recovery."
tags: []
---

# Delivery Watchdog — wake dispatcher + stale-pending alerter

## What it is
A gateway daemon with one resident scan loop. It re-publishes missed pending
wakes, reports delivery stalls, and performs the retained stale-inbound sweeps.
It does not force-terminate or resurrect agents, recover crash-marked owners, or
replay graph work. Config-gated by `AVA_DELIVERY_WATCHDOG_ENABLED`. The jobs
are specified in [[services/docs/gateway_side/delivery_watchdog/jobs/jobs.ava.okf.md]].

**Role affiliation**: gateway side — `ServiceSpec.capabilities=_GATEWAY` in `ops/spec.py`, `requires_db=True` (polls `inbound_messages`). Kept alive by the root supervisor's health monitor through the roster's `/healthz` identity probe.

## Key Dependencies
- [[agent/db/docs/db.ava.okf.md]] — polls `inbound_messages` + reads `agents_meta` owner status
- [[agent/graph/docs/graph.ava.okf.md]] — the claim loop whose lost-wake window this closes
- [[process-lifecycle.ava.okf.md]] — ordinary lifecycle semantics

## Entry Points
- `services/wake/delivery_watchdog/daemon.py` — `.venv/bin/python -m services.wake.delivery_watchdog.daemon`
- `services/wake/delivery_watchdog/dead_letter.py` — stale-inbound dead-letter sweeps (split out at the line budget; re-exported by `daemon.py`)
- Root's health monitor keeps it alive via the roster's `/healthz` identity probe (`ops/roster/healthz.py`)

## Notes
- One instance per cluster (runs on the gateway, owns the data plane)
- Its degraded-WARNING doubles as a dispatcher-health signal: the per-agent 30s recheck warns when it fires, which only happens if the dispatcher is dead AND a wake was lost
- After correcting the underlying delivery failure, manually resume watchdog dispatch with `UPDATE inbound_messages SET dispatch_count = 0, last_dispatch_at = NULL, poisoned_at = NULL WHERE id = <inbound_id>;`.
- Manually clear an agent-level automatic-wake suppression with `UPDATE agents_meta SET wake_suppressed_until = NULL, wake_suppress_reason = NULL WHERE id = <agent_id>;`.
