---
type: doc
title: Re-entry Paths — Restart, Resurrect, Hibernate
description: The three re-entry paths out of a terminated/idling row (restart via restarting, resurrect via on-delivery + crash-resurrect, hibernate swap-in) and the operation-by-operation comparison table.
tags:
- agent
- lifecycle
---

# Re-entry Paths

- **restart**: `running ─CAS→ restarting ─respawn→ idling (unclaimed) ─claim→ running` — never enters terminated (`agent/graph/_claim.py` CAS-flips running→restarting, `/exited` skips 'restarting' rows, restarter daemon calls `ops/agent_wake.py:respawn_agent`)
- **resurrect**: `terminated → idling (unclaimed) ─claim→ running`. ① **on-delivery**: pending work (`chat` or `compact_request`) to a terminated row → gateway `resurrect_if_terminated`; the exact inbound id and expected kind travel to the home runner, whose final transition requires that row still be pending, newer than the current death, and above `last_force_terminate_inbound_id`, so INSERT→concurrent force-kill fails closed. ② **crash-resurrect** (`ops/controllers/resurrect.py:CrashResurrectController`, 30s scan): local agents with `termination_source ∈ {reaper, launch-confirm}` and `pending` **or `claimed`** work-type inbound, past backoff. Its claim carries the exact termination source, `status_changed_at` death epoch, and claim stamp through the final CAS, preventing a user kill or same-source death ABA from being reversed. Only **involuntary deaths** are eligible (reaper=process reclaimed, launch-confirm=startup unconfirmed), never `user` (force-kill) or `exit` (self-exit). `claimed` covers in-progress-only rows: startup reconcile adjudicates (submitted→done; else→pending, redelivered). Both paths hit `ops/agent_wake.py:resurrect_agent` and inject a `resurrect` marker ("resurrected by {resurrected_by}"). Resurrection locks the active home machine before the agent row, creates the detached session under those locks, commits its marker/work atomically with the idling transition, and lets the child claim only after commit. Explicit, pending-work, controller, and retry resurrection all reject a paused home; other re-entry paths retain their existing admission rules and a machine pause's final sweep handles their existing rows.
- **hibernate**: `idling ─SIGUSR1→ (running finalize) → hibernating ─swap-in→ idling (unclaimed) ─claim→ running`. After `AVA_HIBERNATE_IDLE_THRESHOLD_SECONDS` (default 450s, above heartbeat 300s — targets `pause_heartbeat` agents), `ops/controllers/hibernate.py:HibernateController` sends SIGUSR1; agent exits cleanly, row stays hibernating (gateway `/hibernating`, `WHERE status IN ('running','idling')` — on SIGUSR1 `_wait_for_batch`'s finally flips idling→running; **no page close or exit events**). Swap-in via `ops/agent_wake.py:swap_in_agent`, **no lifecycle marker** — agent has zero trace of swap. **Projected as idling** (`ava/_gateway_client.py:_project_ops_status` / `ui/web/src/lib/types.ts:projectAgentStatus`); reaper scans exclude hibernating.


## Restart vs Terminate vs Resurrect vs Hibernate
| Operation | Process | Agent ID | main session record | shell sub-session | Status | agent visible |
|------|------|----------|-----------|-----------------|------|-----------|
| `terminate()` | exit | retained | destroyed | retained | terminated | yes (marker) |
| `restart()` | replace | retained | rebuilt | retained | running (new) | yes (marker) |
| `resurrect()` | new process | retained | rebuilt | retained | running | yes (marker) |
| `update()` | replace | retained | rebuilt | retained | running (new code) | yes (marker) |
| hibernate swap | kill→new process | retained | rebuilt | retained | hibernating→running | **no trace** |


Parent: [[agent/process-lifecycle/process-lifecycle.ava.okf.md|Process Lifecycle]].
