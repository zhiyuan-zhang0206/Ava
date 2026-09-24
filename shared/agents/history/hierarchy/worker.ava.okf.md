---
type: doc
title: The Hierarchy Worker
description: The compact-boundary-triggered build worker (task #3704 P2b) — its event trigger and reconcile scan, claim/drain mechanics, failure handling, regeneration guardrails, cost observability, and knobs.
tags:
- hierarchy
- worker
---

# The Hierarchy Worker (task #3704 P2b)

`services/hierarchy_worker/` is the compact-driven builder. It runs as a
gateway-hosted built-in schedule (`schedules/hierarchy-worker-schedule.py`,
built-in `hierarchy-worker`, product class, enabled by default): one cron
slot a minute calls a tick, which claims and drains due build jobs — each in
its own child process — and runs the reconcile scan only when
`hierarchy_fallback_scan_seconds` has elapsed (the first tick after a
process start always scans), tracking work in `hierarchy_jobs` +
`hierarchy_worker_state`. A master switch (`hierarchy_worker_enabled`,
shipped off) gates both the enqueue and the tick.

- **Triggers** (task #4674): the compact-boundary event is the trigger —
  `mark_compact_boundary` best-effort enqueues one job per new boundary; a
  failed enqueue is a warning + `hierarchy_enqueue_failed`, and the reconcile
  scan backstops lost events, stranded retries and budget continuations (both
  paths idempotent: at most one live job per agent, enforced by a partial
  unique index and an atomic claim; the claim also backfills `include_tail`
  from `scan.first_build` for event-enqueued rows). An agent's first sight
  records the boundary silently — no build for pre-existing history; the
  worker only follows new compactions (backfill is on-demand, P2c).
- **The first build is the full retention window** (one-time and bounded),
  with the trigger-time tail sealed — the same semantics as the manual first
  run (`scripts/build_hierarchy_once.py`). Every later compact-driven pass
  seals no tail and leaves it pending for the next compact.
- **The tail seal** (task #3981 C, opt-in via `hierarchy_tail_seal_enabled`):
  an agent whose newest checkpoint has been quiet for
  `hierarchy_tail_idle_minutes` gets its trailing stretch sealed by a `tail`
  job, so default run-timeline windows show real blocks instead of an
  uncovered tail. Gates: the checkpoint delta since the last seal
  (`hierarchy_worker_state.last_tail_seal_cp_id`), a per-agent interval (or
  the ordinary backoff / continuation pacing), a per-tick cap, and the
  precondition that a clean non-tail build exists — it continues coverage,
  never initializes it. Tail cells are provisional: a rebuild re-cuts them;
  compact-sealed cells reproduce.
- **Slicing and the zero-redo invariant**: a job self-limits at
  `hierarchy_job_budget_seconds`, newest stretches first; the unattempted
  remainder is recorded as `skipped` and the continuation run replays the
  (pure, cheap) seal cascade, skips everything already materialized via the
  hash cache, and continues there.
- **Failure handling**: node failures ride the job row's scope stats and are
  retried by the next run; a crashed or killed job is recovered by the parent
  process or the stale-running sweep, and non-clean retries back off
  exponentially (base/cap configurable). A budget-truncated continuation
  drains immediately. A regen-halt cut (task #4674) instead carries a
  non-null `error` marker on its `done` row — it never counts as a build
  (`scan.first_build` / `scan._has_clean_baseline`) and its continuation
  waits out the backoff rather than draining.
- **Guardrails** (task #4674, all `settings.daemon.hierarchy_*`): a done-time
  size alert past `hierarchy_regen_alert_nodes_per_job`; a mid-run cumulative
  node cap (`hierarchy_regen_halt_nodes_per_job`) that leaves the remainder
  `skipped` and marks the row (see Failure handling); a low-reuse signal
  whose face is paired with the size threshold; and the fleet-wide 24h
  generated-node budget (`hierarchy_regen_daily_budget_nodes`) — crossing it
  trips `hierarchy_worker_breaker` and claiming stops until an operator
  resets it (`reset_at` + `reset_note`), re-arming only after a cooled
  window. First builds and tail seals are exempt from the per-job thresholds.
- **Knobs** (`settings.daemon.hierarchy_*`, each with its written reason):
  job budget, hard deadline, retry base/cap, generation concurrency, the
  master switch and reconcile cadence, the regen alert / halt / daily-budget
  thresholds and the low-reuse ratio (task #4674), and the child-kill /
  stale-row graces; the generation model is the target agent's own effective
  model (`shared.agent_snapshot.agent_effective_model` — overlay preferred,
  fleet default else), with `settings.lm.hierarchy_model` as the last-resort
  fallback.
- **Cost observability**: each job row records the run's scope (stretches,
  nodes generated/reused/failed/skipped) and its token sums; the LLM usage
  ledger (`usage_source='hierarchy.generate'`) is the authoritative per-call
  record. Both worker processes boot the logging/telemetry seam — `job.main`
  as `hierarchy-worker`, the host's `prepare()` as
  `schedule-hierarchy-worker` — so the ledger rows and the worker's own
  records actually reach the event stream (task #3868).
- **Model lifecycle** (task #3915): a build run constructs its generation
  model once — the worker's job child (or the manual script) builds it up
  front and closes its provider client when generation ends — so no HTTP
  sockets linger past the run.
