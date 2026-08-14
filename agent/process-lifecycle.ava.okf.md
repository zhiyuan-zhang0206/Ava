---
type: doc
title: Process Lifecycle
description: "Agent process lifecycle: spawn to terminate, signal handling, exit tracking, gateway notification."
tags: []
---

# Process Lifecycle

## Lifecycle State Machine
Main path:
```
allocated → starting → running ⇄ idling → terminated
                                  idling ⇄ hibernating   (operational layer internal state)
```
- `allocated → terminated`: boot failure (schema gate, `agent/_starting.py:_mark_allocated_terminated`, `WHERE status='allocated'`) or a launch that never confirmed (`ops/agent_launch.py`, both the off-path confirm and the retries-exhausted write, same predicate). **Every write out of `allocated` carries `WHERE status='allocated'`** — the row is contested by the child's own claim, and an unguarded terminate buries a live agent, then crash-resurrect launches a duplicate for it.
- **launch confirm** (`ops/agent_launch.py:_wait_for_status_to_leave_allocated`): the launcher polls the row for `AVA_LAUNCH_CONFIRM_TIMEOUT_SECONDS` (45s) waiting for the child's `allocated → starting` CAS. The pre-flip segment (python start, imports, schema assert, placement SELECT) is invisible in the row, so the launcher asks the **supervisor's session record** whether the process is alive — every ~1s, and again at the deadline: alive → ONE extension to `AVA_ALLOCATED_REAP_GRACE_SECONDS` (120s, where the reaper takes over); dead → fail now (after re-reading the row, so a child that claimed and exited inside the probe interval still counts as started). `termination_source='launch-confirm'` either way, so a genuine failure is crash-resurrect-eligible.
- **boot deadline** (`agent/_boot_deadline.py`): what makes the liveness answer above worth reading. The child arms a watchdog before its import chain and exits on whichever bound trips first — stall (30s, reset by every `_boot_timing` phase) or budget (90s, never reset) — so in the pre-flip window **alive ⇒ progressing**. The bounds arrive as `--boot-stall-seconds` / `--boot-budget-seconds` **argv**; the `AVA_AGENT_BOOT_*` env names are launcher-side settings aliases — setting them on the agent process env has no effect. Ordering pinned in `tests/shared/test_config.py`: stall (30) < confirm (45) < budget (90) < reap grace (120); the first makes the launcher's probe decisive, the last keeps the reaper from ever meeting a live child (its clock `status_changed_at` resets only on a status flip, so pre-flip progress cannot hold it off). The child does **not** write its own row on overrun — a boot wedged on the data plane cannot be relied on to reach it.
- `starting → terminated`: boot-stage failure
- `running/idling → terminated`: signal / exit (gateway `/exited` finalize, `WHERE status IN ('starting','running','idling')`)
- **restart**: `running ─CAS→ restarting ─respawn→ allocated → starting → running` — never enters terminated (`agent/graph/_claim.py` CAS-flips running→restarting, `/exited` skips 'restarting' rows, restarter daemon calls `ops/agent_wake.py:respawn_agent`)
- **resurrect**: `terminated → allocated → starting → running`. ① **on-delivery**: inbound (chat/compact_request) to terminated row → gateway `resurrect_if_terminated` ② **crash-resurrect** (`ops/controllers/resurrect.py:CrashResurrectController`, 30s scan): local agents with `termination_source ∈ {reaper, launch-confirm}` and `pending` **or `claimed`** work-type inbound, past backoff. Only **involuntary deaths** (reaper=process reclaimed, launch-confirm=startup unconfirmed); never `user` (force-kill) or `exit` (self-exit). `claimed` covers in-progress-only rows: startup reconcile adjudicates (submitted→done; else→pending, redelivered). Both paths hit `ops/agent_wake.py:resurrect_agent` and inject `resurrect` marker ("resurrected by {resurrected_by}"), resurrected_by = initiator / `"system"`.
- **hibernate**: `idling ─SIGUSR1→ (running finalize) → hibernating ─swap-in→ allocated → starting → running`. After `AVA_HIBERNATE_IDLE_THRESHOLD_SECONDS` (default 450s, above heartbeat 300s — targets `pause_heartbeat` agents), `ops/controllers/hibernate.py:HibernateController` sends SIGUSR1; agent exits cleanly, row stays hibernating (gateway `/hibernating`, `WHERE status IN ('running','idling')` — on SIGUSR1 `_wait_for_batch`'s finally flips idling→running; **no page close or exit events**). Swap-in via `ops/agent_wake.py:swap_in_agent`, **no lifecycle marker** — agent has zero trace of swap. **Projected as idling** (`ava/_gateway_client.py:_project_ops_status` / `frontend/src/lib/types.ts:projectAgentStatus`); reaper scans exclude hibernating.

## Signal Handling (`agent/lifecycle.py:_install_lifecycle_signal_handlers()`)
- **SIGHUP** → `SystemExit("signal:SIGHUP")`: kept as a **defensive** catch — agents are detached native processes, so the old session-close SIGHUP no longer exists; a stray one still exits cleanly through the normal tracking path
- **SIGTERM** → `SystemExit("signal:SIGTERM")`: external kill
- **SIGUSR1** → hibernation: handler **sets `_hibernate_requested=True` synchronously** then raises `SystemExit`. Flag needed: asyncio converts handler's SystemExit to `CancelledError`; by `main()` finally, `sys.exc_info()` is CancelledError. `_exit_reason()` uses flag → `"hibernate"` → finally routes to `/hibernating` not `/exited`.
- All three → `SystemExit`, ensuring `main()`'s `finally` executes.

## Exit Reason Tracking (`agent/lifecycle.py:_exit_reason()`)
Called in `main()` finally, derives from `sys.exc_info()`:
- `signal:SIGHUP` / `signal:SIGTERM` — signal-triggered
- normal exit — no exception
- other exceptions — exception type name

## Gateway Notification (`agent/lifecycle.py:_notify_exit()`)
Notifies gateway on exit: gateway updates agents_meta to 'terminated', closes agent's UI pages. Agent only notifies; gateway owns writes.

## Session Retention (shell sub-sessions only)
Shell sub-sessions (`ava-agent-<id>-shell-<n>[-<name>]`) survive lifecycles; the **main process is a plain native process** (native supervisor) — retention rules + the session-record model live in [[sessions.ava.okf.md]]. Silent death = wedged/killed process, reaped by the restarter and crash-resurrected.

## Restart vs Terminate vs Resurrect vs Hibernate
| Operation | Process | Agent ID | main session record | shell sub-session | Status | agent visible |
|------|------|----------|-----------|-----------------|------|-----------|
| `terminate()` | exit | retained | destroyed | retained | terminated | yes (marker) |
| `restart()` | replace | retained | rebuilt | retained | running (new) | yes (marker) |
| `resurrect()` | new process | retained | rebuilt | retained | running | yes (marker) |
| `update()` | replace | retained | rebuilt | retained | running (new code) | yes (marker) |
| hibernate swap | kill→new process | retained | rebuilt | retained | hibernating→running | **no trace** |

## Key Dependencies
- [[sessions.ava.okf.md]] — session retention
- [[db.ava.okf.md]] — agents_meta updates
- [[loop.ava.okf.md]] — main() finally calls lifecycle hooks

## Entry Points
- `agent/lifecycle.py:_install_lifecycle_signal_handlers()` — signals → SystemExit (SIGUSR1 → hibernate flag)
- `agent/lifecycle.py:_exit_reason()` — exit reason tag (hibernate flag takes priority)
- `agent/lifecycle.py:_notify_exit()` / `_notify_hibernate()` — gateway (`/exited` → terminated / `/hibernating` → hibernating)
- `agent/loop.py:_route_process_end_notify()` — finally routes to exit or hibernate
- `ops/controllers/hibernate.py:HibernateController` — swap-out scan (SIGUSR1) + swap-in (`swap_in_agent`)
- `ava.self.terminate()` / `ava.self.restart()` — SDK (hibernation **no SDK**, operational only)

## Notes
- All lifecycle hooks called from `main()`'s `finally` — even silent deaths leave a trace
- `resurrect()` recovers from terminated, not cold start — memory and workspace survive
