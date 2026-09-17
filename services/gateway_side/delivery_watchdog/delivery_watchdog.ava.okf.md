---
type: doc
title: Delivery Watchdog — wake dispatcher + stale-pending alerter
description: "Gateway-owned wake dispatcher (re-publishes the Redis wake for stale pending inbounds every 0.5s) + stale-pending alerter + terminated-owner resurrect retry + stale-claimed dead-letter sweep + stalled crash-marked harvest request + hosted-turn liveness recovery — the cluster-wide delivery tripwire (Task #689 G4, user ruling 2026-08-03; Task #654; Task #3618)."
tags: []
---

# Delivery Watchdog — wake dispatcher + stale-pending alerter

## What it is
A gateway daemon with six jobs on one fast tick (user-confirmed design 2026-08-02, `delivery-dispatcher-design-2026-08-02.md`): it is the cluster-wide tripwire that a `pending` inbound actually reaches its owner. Config-gated by `AVA_DELIVERY_WATCHDOG_ENABLED`. The six job families — wake dispatch, stall alerting, terminated-owner resurrect retry, stale-inbound dead-letter sweeps, stalled crash-marked recovery request, and hosted-turn liveness recovery — are specified in [[services/gateway_side/delivery_watchdog/jobs.ava.okf.md]].

**Role affiliation**: gateway side — `ServiceSpec.capabilities=_GATEWAY` in `ops/spec.py`, `requires_db=True` (polls `inbound_messages`). Kept alive by `services/healthchecks/delivery_watchdog.py` (gateway watchdog).

## Key Dependencies
- [[db.ava.okf.md]] — polls `inbound_messages` + reads `agents_meta` owner status
- [[agent/graph/graph.ava.okf.md]] — the claim loop whose lost-wake window this closes
- [[process-lifecycle.ava.okf.md]] — resurrect semantics the retry re-runs

## Entry Points
- `services/delivery_watchdog/daemon.py` — `.venv/bin/python -m services.delivery_watchdog.daemon`
- `services/delivery_watchdog/dead_letter.py` — job 4's stale-inbound dead-letter sweeps (split out at the line budget; re-exported by `daemon.py`)
- Watchdog keeps alive via `services/healthchecks/delivery_watchdog.py`

## Notes
- One instance per cluster (runs on the gateway, owns the data plane)
- Its degraded-WARNING doubles as a dispatcher-health signal: the per-agent 30s recheck warns when it fires, which only happens if the dispatcher is dead AND a wake was lost
- After correcting the underlying delivery failure, manually resume watchdog dispatch with `UPDATE inbound_messages SET dispatch_count = 0, last_dispatch_at = NULL, poisoned_at = NULL WHERE id = <inbound_id>;`.
- Manually clear an agent-level automatic-wake suppression with `UPDATE agents_meta SET wake_suppressed_until = NULL, wake_suppress_reason = NULL WHERE id = <agent_id>;`.
