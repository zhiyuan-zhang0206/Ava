---
type: doc
title: Hosted-turn liveness recovery
description: Delivery-watchdog job 6 — wedge detection for hosted `running` turns against the agent-host progress heartbeat, and the committed recovery wake (task #1712).
tags: []
---

# Hosted-turn liveness recovery

**Hosted-turn liveness recovery** (`turn_liveness.py`, task #1712) — every round, confirm each hosted `running` agent whose DB activity is older than the 2400 s wedged-agent budget (`wedged_agent_inbound_age_seconds`) against the agent-host's independent 15 s Redis progress heartbeat (`host_turn_progress:<machine>`, 60 s TTL): a missing heartbeat or equally stale per-turn marks is a wedged turn. Recovery force-terminates the incarnation and commits the marked `hosted_turn_recovery` chat in the terminating transaction itself (`terminate_agent_op(recovery_wake=...)`; the wake's id sits above the force fence and its `created_at` is the statement clock, both of which the resurrection trigger requires) — so a committed recovery always has its wake, a failed insert rolls the force back and leaves the agent running and wedged for a later scan, and guarded resurrection retries survive restarts; the marker is the system-notice carve-out (task #3687) that lets the recovery wake its owner — one attempt per agent per persisted 10-minute cooldown, at most four recoveries at once, with `host_turn_stall_detected` evidence.
