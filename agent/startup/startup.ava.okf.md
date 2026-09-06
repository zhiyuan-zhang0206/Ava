---
type: doc
title: Startup Sequence
description: The startup sequence of an agent process from being spawned to entering the run loop — a multi-stage orchestration minimizing spawn latency (~5s → ~1-2s) while ensuring state consistency.
tags: []
---

# Startup Sequence

## What it is

The **startup sequence** of an agent process from being spawned to entering the run loop. This is a carefully orchestrated multi-stage process aiming to minimize spawn latency (from ~5s to ~1-2s) while ensuring state consistency.

## Core Responsibilities

### Startup Stages

```
gateway spawn → unclaimed 'idling' → claim 'running' → heavy import → run loop
                                      ↑ boot stage ↑
```

### Stage 1: Schema Gate (`agent/_starting.py:claim_agent_row_or_die_on_stale_schema()`)
Validate schema before claiming running. Rejected owned attempts cannot settle
another runtime or durable command. Exact rejection and launch-record contracts:
[[admission.ava.okf.md]].

### Stage 0: Arm the boot watchdog (`agent/__main__.py` → `_boot_deadline.arm()`)
- Runs before the `_starting` import, so it covers the import chain itself — which is why the window arrives on argv (`--boot-stall-seconds`, from `settings.gateway.agent_boot_stall_seconds` via `ops/agent_launch.py`) and not from `shared.config`: importing that module is part of what needs watching
- `_boot_deadline.consume_flags()` **strips the flags from `sys.argv`**. `agent/loop.py:run()` parses the remainder with a strict `parse_args()` and runs after the row is claimed, so a flag left behind is a `SystemExit(2)` for an agent whose row reads 'running' under a departing pid. A malformed value disables the watchdog rather than the agent — the launcher's confirm still bounds the launch
- A daemon thread watches `_boot_timing.progress()`; no new phase within the window → name the stalled phase on stderr and `os._exit`. Patience follows progress, so a slow boot is never cut off and a wedged one dies one window after its last real step
- A second bound, `--boot-budget-seconds` (90s), caps the WHOLE pre-claim boot and never resets. The stall window alone bounds a boot only at `phases x stall` — today 4 x 30s, exactly the reap grace — so without it a slow-but-progressing child has its row reaped while still alive. Whichever bound trips first wins; the stderr line names which
- Disarmed immediately after the CAS — past the claim the row carries a pid and the restarter's reapers are the authority

### Stage 2: Claim (`agent/_starting.py:claim_agent_row()`)
- Flip an unclaimed agents_meta row from 'idling' to 'running'
- Record pid, publish `agent_updated` event
- **Write the liveness lease** (`lease_expires_at = now() + TTL`) in the same UPDATE — the claim is the lease's birth; the run loop renews it ([[../lease.ava.okf.md|Agent Liveness Lease]])
- **Do not import any langgraph/langchain** — minimize delay to claim the row
- Durable command identity, fixed deadline/attempt budget, canonical publication
  and explicit cold acceptance: [[admission.ava.okf.md]].

### Stage 3: Boot Stage
- This stage is between '_starting' and run loop
- Executes import chain (langgraph, agent modules, etc.)
- `_boot_timing.py`: records phase durations
- `startup.py`:
  - `_wrap_saver_writes_with_loud_failure()` — monkey-patch checkpointer to make write failures observable
  - `_wrap_saver_writes_with_nstep_interval()` — persist the selected super-steps and flush the skipped tail at the turn boundary. The final flush removes its in-memory tail only after the underlying save succeeds; errors and cancellation remain visible and retain the same checkpoint for retry. Writes and flushes serialize on that checkpoint thread without blocking other agents. A repeated successful flush is a no-op; a flush that failed is not durability acknowledgement. This is in-process retry state, not a replacement for crash recovery from the last persisted checkpoint.
  - `_reconcile_claimed_inbounds_at_startup()` — clean up 'claimed' inbounds left by previous process
  - `_repair_dangling_tool_use_at_startup()` — repair crash-left tool pairing: synthesize an interrupted result for an unpaired use or drop an orphan result
  - `_apply_per_agent_framework_config()` — apply this agent's two stored config maps onto the settings singleton, birth stamp first and explicit overlay on top, so the effective order is `config_overlay > birth_config > current config`. Runs before `build_chat_model` so a per-agent model reaches the LLM client this process actually builds. Both maps arrive via child-process env (`$AVA_AGENT_CONFIG_OVERLAY` / `$AVA_AGENT_BIRTH_CONFIG`), **never argv** — argv is world-readable via `ps` and can carry a provider api_key (issue #974); launcher reads them off `agents_meta`; `birth_config` is the frozen-lifecycle set stamped at spawn (see the `shared/config` module docstring for the axis and `shared/birth_config.py` for the mechanics)
  - `_write_effective_config_to_restart_completed()` — record the post-apply config snapshot
  - `_notify_screen_capture_at_startup()` — renders whichever OS-level screen-capture fault the converge preflight recorded (permissions helper holds no Screen Recording grant / never answered, so the grant is unknown); each state carries its own headline and fix
  - [[../page-restore.ava.okf.md]] — page restore

### Stage 4: Run Loop
- Enter `agent/loop.py` main loop; no second status transition is needed

## Key Dependencies

- [[../env-vars.ava.okf.md]] — read environment variables at startup
- [[../loop.ava.okf.md]] — run loop entry point
- [[../db.ava.okf.md]] — agents_meta table, schema version table

## Entry Points

- `agent/_starting.py:claim_agent_row_or_die_on_stale_schema()` — earliest startup point
- `agent/startup.py` — multiple one-time helpers
- `agent/_boot_timing.py` — startup timing tracking; also the boot's progress signal (`progress()`), read by the watchdog below
- `agent/_boot_deadline.py` — the child's own boot watchdog: exits the process when no new phase is reached within `--boot-stall-seconds`, so "the process is alive" means "the boot is progressing"
- `agent/warmup.py` — one-time warm-up module (`.venv/bin/python -m agent.warmup`), triggered by `ava start` detached (`cli/commands/_warmup.py`), **not** in the per-agent startup path
- `agent/loop.py:main()` — agent_id set + run loop startup

## Notes

- The schema gate MUST run before the claim — a post-claim failure is classified as a boot-phase death instead of a pre-claim rejection.
- **Everything before the claim is invisible in the row** — python startup, this module's imports, `assert_schema_current`, the placement SELECT all run while the row still reads unclaimed 'idling' with no pid, indistinguishable from a launch that never started. The launcher therefore never learns from the row that a pre-claim boot is progressing; it asks the supervisor whether the process still exists (`ops/agent_launch.py`), and **the child makes that answer mean something** by exiting when its own boot stops progressing — `agent/_boot_deadline.py`, armed in `agent/__main__.py` before the import chain and disarmed after the CAS. Anything added here lengthens the segment but no longer widens a blind spot, provided it marks a phase
- **Every pre-claim step must call `_boot_timing.mark()`** — those marks are the boot watchdog's only evidence of progress (`_boot_timing.progress()`), and reaching one is what buys the child another `AVA_AGENT_BOOT_STALL_SECONDS`. An unmarked step is indistinguishable from a wedge and will be killed as one once it runs longer than that window. Pre-claim phases today: `start` → `starting_import` → `schema_check` → `placement_check` → `claim_row`
- `claim_agent_row()` is designed to be fast — no heavy dependency imports, only DB writes
