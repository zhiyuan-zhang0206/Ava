---
type: doc
title: Update straggler reap — truncation, honest receipts, successor settle
description: An update-family drain truncates and releases a cohort member still un-landed past its restart window as `reaped`, and a successor boundary settles the mark so the agent re-runs on the new code.
status: current
---

# Update straggler reap — truncation, honest receipts, successor settle

An update-family drain (a rollout's Phase A, `spawn_update`) may reap its
stragglers (task #4016; user ruling 2026-09-19): a native hosted cohort member
still un-landed `update_straggler_reap_seconds` (default 15; 0 disables) after
ITS restart command was issued is truncated and released with the honest
`reaped` outcome instead of aborting the wave. Interactive pause/stop/restart
drains never reap.

The kill is a durable mark, not a signal. `ops.agent_pause` CAS-marks the row
`restarting` (from `running`, against the observed owner/generation);
`agent.db.has_pending_interrupt` reads that mark together with the member's
still-un-applied maintenance restart as an in-flight abort signal, so the
running exec/LLM node truncates within its existing poll cadence and the exec
subsystem settles its child tree. The mark's status predicates fence every
old-incarnation write path (claim acceptance, lifecycle apply, settle, corpse
stamp), so a dying turn cannot clobber the reap. Its status fence can still
raise an ownership exception while the lease is fresh. A failure recorded
between the committed mark and the reap receipt remains audit evidence;
`MaintenanceHold.unsettled_failures()` excludes reap-certified members in
either receipt order, including when the raw journal is read by out-of-band
triage. The reap receipt must not refuse that already-committed mark.

Receipts are honest end to end. The member lands in `MaintenanceHold.reaped`
(a receipt deliberately not `drained`, never a fabricated flush/apply);
`pending_command` excludes it from held-control wakes; `verify_drained`
certifies it by the reap state — the row still `restarting` and its command
never applied nor observed — and rejects drift. A refused reap aborts the
drain with the hold retained, exactly like the timeout it replaces, and the
wave report carries a `reaped` count line next to the telemetry row.

The mark is settled at a successor boundary: the agent-host boot, or the local
unpause (`shared/straggler_reap.settle_stranded_reaps[_async]`, called from
`ops.cluster_pause.unpause_local_cluster` — the compensating resume of an
aborted wave runs while the host stayed up). Settlement closes the
never-applied command as `done` with
`lifecycle_result={"outcome": "reaped", "reason": "update_straggler_reap"}`,
returns the row to `idling` with ownership and lease released, and the caller
wakes the agent once. That first admission runs the existing inbound reconcile
(uncommitted claimed ordinary rows return to `pending` and are re-delivered)
and the dangling-tool repair — the truncated work re-runs on the new code,
at-least-once, with side-effect replay accepted by the ruling.

Rows under an external takeover (`agent_impersonations` requested / accepted /
live-active) are never reaped; W is decoupled from `exec_timeout_seconds`; and
a reaped turn blocked where asyncio cannot interrupt it is bounded by the
wave's own stop leg — the drain never waits on it.

One low-probability boundary is accepted rather than guarded (task #4027/B1):
a stranded mark whose row first goes cold can be consumed by the cold
normalization itself — with the host absent, a `host_absent` prepare runs and
the row's checkpoint happens to meet the persisted cold-END gates of
`shared.maintenance_cold.normalize_retired_intent(restarting=True)`, whose
UPDATE moves the row to `idling`. The settle selector (which requires
`restarting`) then no longer matches and the un-applied maintenance restart
remains; that residue settles through the ordinary lifecycle path — at worst
one late restart, never a silent loss. Accepted at the 2026-09-19 review; the
clear-exit enumeration guard
(`tests/ops/test_straggler_reap.py::test_restarting_mark_exits_are_enumerated`)
already names `shared.maintenance_cold.py` as the second exit.
