---
type: doc
title: Delivery Watchdog — wake dispatcher + stale-pending alerter
description: "Gateway-owned wake dispatcher (re-publishes the Redis wake for stale pending inbounds every 0.5s) + stale-pending alerter + terminated-owner resurrect retry + stale-claimed dead-letter sweep — the cluster-wide delivery tripwire (Task #689 G4, user ruling 2026-08-03; Task #654)."
tags: []
---

# Delivery Watchdog — wake dispatcher + stale-pending alerter

## What it is
A gateway daemon with four jobs on one fast tick (user-confirmed design 2026-08-02, `delivery-dispatcher-design-2026-08-02.md`): it is the cluster-wide tripwire that a `pending` inbound actually reaches its owner. Config-gated by `AVA_DELIVERY_WATCHDOG_ENABLED`.

**Role affiliation**: gateway side — `ServiceSpec.capabilities=_GATEWAY` in `ops/spec.py`, `requires_db=True` (polls `inbound_messages`). Kept alive by `services/healthchecks/delivery_watchdog.py` (gateway watchdog).

## Core Responsibilities
1. **Wake dispatch** — every `AVA_DELIVERY_WATCHDOG_INTERVAL_SECONDS` (default 0.5s), re-publish the Redis wake for every `pending` inbound whose owner is `idling` and whose row is older than `AVA_DELIVERY_WATCHDOG_DISPATCH_THRESHOLD_SECONDS` (default 1s). Per-inbound dispatch counts apply the configured backoff ladder; after the cap, the watchdog poisons the row, emits `delivery_poisoned`, and stops re-publishing it without preventing a recovered agent from claiming it. An owner whose `agents_meta.wake_suppressed_until` has not expired is excluded while new messages continue to queue as `pending`. A lost publish (pub/sub is fire-and-forget) is retried promptly instead of waiting out the claim loop's 30s SELECT recheck, while a permanently failing inbound cannot create an unbounded wake storm. Load is independent of fleet size; the per-agent 30s recheck stays as the double-fault safety net.
2. **Stall alerting** — WARNING each chat inbound still `pending` past `AVA_DELIVERY_WATCHDOG_THRESHOLD_SECONDS` (default 30s) whose owner is waiting/terminal (idling / terminated). Once-per-row while it stays pending (pruned memory set).
3. **Terminated-owner resurrect retry** — every tick, for each DISTINCT terminated agent holding a `pending` chat created **after its current `status_changed_at` death** and with an id greater than `last_force_terminate_inbound_id`, re-run `resurrect_if_terminated` with that exact chat id and kind (delivery-path auto-resurrect retry, Task #689 G4). The home runner's final terminated → idling CAS re-checks all of those predicates, so an old/claimed row or an RPC racing a later explicit kill cannot reverse the kill. A per-agent in-flight task map prevents a slow RPC from being queued again across ticks; completed attempts then enter the 60s per-agent cooldown. After five consecutive attempts still return `terminated`, the watchdog writes `wake_suppressed_until` with a 30-minute exponential window capped at 24 hours, records reason `resurrect_failed`, and emits one `delivery_wake_suppressed` anomaly. The durable selector excludes that owner until expiry. A successful spawn or successful inbound claim clears both suppression columns; an auto-resurrect success also resets the watchdog's in-memory failure and escalation counts. The per-tick cap limits new eligible-owner admissions and the two-way semaphore limits active RPC concurrency; in-flight/cooling/suppressed owners do not consume a later tick's admission cap.
4. **Stale-claimed dead-letter sweep** — every 30s, flip `claimed` chat inbounds of TERMINATED owners older than `AVA_DELIVERY_WATCHDOG_STALE_CLAIMED_THRESHOLD_SECONDS` (default 24h; age from `claimed_at`, falling back to `created_at` for pre-2026-08-02 rows) to `done`. Terminated agents leave claimed rows behind (reconcile runs only at boot); a resurrect would otherwise flip them all to `pending` and re-deliver ancient messages (Task #654). The reconcile-side cutoff (`agent/db.py::reconcile_claimed_inbounds`) applies the same threshold at boot, closing the resurrect race at the source.

The same resurrection retry owner also resumes an `idling` allocation with no
PID only when an existing server-prepared resurrect inbound binds this exact
pending chat, allocation epoch, old incarnation, machine and unexpired deadline.
It uses the same in-flight map, cooldown and RPC budget. The home resumes that
allocation without replacing its identity or resetting the durable OS counter;
ordinary idle agents and historical task-owner records do not qualify.

`running` owners are never dispatched or alerted — a chat queued behind a long in-flight turn is normal; the claim's turn-end SELECT picks it up. `restarting` is left to its own reaper; unclaimed idling rows have no owner.

## Key Dependencies
- [[db.ava.okf.md]] — polls `inbound_messages` + reads `agents_meta` owner status
- [[agent/graph/graph.ava.okf.md]] — the claim loop whose lost-wake window this closes
- [[process-lifecycle.ava.okf.md]] — resurrect semantics the retry re-runs

## Entry Points
- `services/delivery_watchdog/daemon.py` — `.venv/bin/python -m services.delivery_watchdog.daemon`
- Watchdog keeps alive via `services/healthchecks/delivery_watchdog.py`

## Notes
- One instance per cluster (runs on the gateway, owns the data plane)
- Its degraded-WARNING doubles as a dispatcher-health signal: the per-agent 30s recheck warns when it fires, which only happens if the dispatcher is dead AND a wake was lost
- After correcting the underlying delivery failure, manually resume watchdog dispatch with `UPDATE inbound_messages SET dispatch_count = 0, last_dispatch_at = NULL, poisoned_at = NULL WHERE id = <inbound_id>;`.
- Manually clear an agent-level automatic-wake suppression with `UPDATE agents_meta SET wake_suppressed_until = NULL, wake_suppress_reason = NULL WHERE id = <agent_id>;`.
