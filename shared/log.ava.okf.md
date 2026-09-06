---
type: doc
title: Logging
description: '`shared/log.py` is the single structural logging module spanning kernel / gateway / SDK subprocesses / all daemons. A global loguru logger singleton, with per-process entry `init_*` called once to bind process-level fields and assemble three sink types: stderr / JSONL file / the unified event pipeline (`shared/telemetry.py` → `events` table).'
tags:
- shared
- library
- observability
---

# Logging

## What is it

`shared/log.py` is the single structural logging **module** (not a package) spanning kernel / gateway / SDK subprocesses / all daemons. A global loguru logger singleton, with per-process entry `init_*` called once to bind process-level fields (`agent_id`) and assemble sinks. All `from shared.log import logger` get the same logger that automatically carries these fields.

`agent_id` is the one field bound **deferred** rather than frozen: `init_agent_process` binds `shared/turn_identity.py:TURN_SCOPED_AGENT_ID`, which resolves per record — turn contextvar, else this process's agent, else the `-` sentinel (an explicit `logger.bind(agent_id=N)` still wins outright). Identical to a fixed binding with one agent per process; it is what lets a process hosting several agents' turns attribute each record to the turn that wrote it. `shared/telemetry.py:emit` applies the same order.

Every log line is also an **event** in the unified event stream (event-system design §1): the loguru side derives `(ts, agent_id, level, event, payload, source)` and enqueues into `shared/telemetry` — the unified emitter — which batch-writes the `events` table (the legacy `agent_events` mirror was removed with the migration window). Business (audit) events flow through the same emitter via `shared/audit_events.py`.

## Core Responsibilities

### Four process entry points
- `init_agent_process(agent_id)` — kernel: stderr + file `agent-{N}.log` + unified event pipeline (process=`agent-kernel`).
- `init_subprocess_logger(agent_id)` — exec subprocess: **only** file sink, no stderr (subprocess stderr is captured by the parent and injected as exec_output fed to the LLM; framework logs on stderr would pollute the agent context). Writes the same `agent-{N}.log`.
- `init_gateway_process(name)` — gateway and every long-running daemon (restarter / watchdog / labeler / memory_indexer / heartbeat / events_maintenance / task_maintenance / ops——**no "runner" daemon**; the only long-running daemon on the agent-runner side is `ops`=agent_ops): stderr + `<name>.log` + unified event pipeline (process=`name`, agent_id NULL on rows); each daemon has its own `<name>.log` for easier postmortem. Also freezes this process's commit — earliest shared seam, see `shared/process_sha.py`.
- `init_cli_process(name)` — detached CLI subprocess (`spawn_update` / schema reconcile): same sink set as gateway; interactive CLI (TTY) skips.
- All are **idempotent** (`_init_done` process-level guard) — `logger.add` is not idempotent; repeated calls accumulate sinks until fd exhaustion (errno 24); watchdog reusing healthcheck every 60s would hit this, the guard blocks it.

### Three sink types
- **stderr**: human-readable colored format `_HUMAN_FORMAT` (`time level a=agent_id message`), aligned with terminal scrollback habits — **not logfmt**.
- **File**: JSONL (`serialize=True` serializes the entire record as a single JSON line), rotated at 100MB / kept for 7 days (`_add_file_sink`). Without rotation `gateway.log` once grew to ~900MB.
- **Unified event pipeline**: `_postgres_sink` derives each INFO+ record to an event and enqueues it into `shared/telemetry` (bounded queue + drain thread). The emitter batch-writes the `events` table (unified schema: `ts / trace_id / span_id / agent_id / machine / process / category / kind / level / source / target_agent_id / attributes`), with `trace_id`/`span_id` captured from the active OTel span at enqueue time (turn_span correlation). `event` value priority: `extra["event"]` → `extra["label"]` (backwards-compatible with `[{label}]` old style) → `"log"`. `payload` = extra minus dedicated columns + `msg` (original text); when `logger.opt(exception=True)` is used, automatically merges traceback / exception_type / exception_value into payload (with a guard to distinguish real exceptions from loguru's empty RecordException `NoneType: None`). `source` = `extra["source"]` (default `"system"`).

### Two key mechanisms
- The seven-day full, 90-day rollup-source and 365-day lineage JSONL mirrors preserve Loki-stable IDs; [[services/gateway_side/events_maintenance/events_maintenance.ava.okf.md|events maintenance]] replays the rollup tier.
- The emitter's bounded queue + daemon drain thread (`shared/telemetry._EventPipeline`) replaces loguru's `enqueue=True`, which uses `multiprocessing.SimpleQueue` allocating POSIX named semaphores; when an agent is SIGKILLed (routine operation) they leak permanently, eventually hitting `kern.posix.sem.max`, after which new agent startups fail with errno 28. The thread queue uses no kernel resources. When the DB is temporarily unavailable, that batch is dropped (`catch=True`); the JSONL file sinks and the emitter's own day-stamped JSONL mirror (`$AVA_HOME/logs/events-YYYYMMDD.jsonl`) serve as durable fallback. Queue-full shedding is counted and reported as one `event_log_drop` event per flush (the ops monitor panel's backlog metric).
- `_StdlibInterceptHandler`: routes records from stdlib `logging.getLogger(...)` into loguru sinks (many services historically used stdlib logging), otherwise their lines would only appear on stderr and not enter the event stream. Installed on the root logger; `psycopg.pool` recycling noise is gated to ERROR.

## Two surfaces, and where they diverge

The sinks produce two places to look — the file `$AVA_HOME/logs/<name>.log` and
the unified event stream (`events` table, which the Stats Dashboard and
`GET /api/cluster/admin/events` read) — carrying the same lines. Two exceptions:

- **milvus** has no event pipeline: it is `execvp`ed into a C++ binary that cannot honor
  loguru wiring, so its daemon `dup2`s the log fd over stdout/stderr before exec.
- **the CLI** initializes only when `AVA_CLI_LOG_NAME` is set —
  `ops/cluster_deploy.py:spawn_update` exports it for the whole updater child
  chain, so a **detached** `ava
  update` child reaches both surfaces, while an interactive `ava status` skips
  init (no event row per command). The CLI's own `print()` stays stdout/stderr,
  captured only in the parent's `spawn-update-<ts>.log`.

**Crash diagnosability**: every daemon wraps `asyncio.run(main())` in a top-level
`except Exception` that `logger.exception(...)`s before re-raising, so a crash
leaves a traceback in the file instead of vanishing into terminal scrollback.

## Event-table partitioning

The `events` table is declaratively RANGE-partitioned by
month on `ts`; the events-maintenance daemon keeps the current and next month
ahead of the write frontier so nothing strands in the DEFAULT catch-all.
Retention DROP is not yet enabled — the row count is still unbounded — but the
structure is what makes reclaiming disk an O(1) partition DROP instead of a
row-by-row DELETE. The day-grain rollups (`agent_metrics_daily` /
`agent_model_tokens_daily`) exist so since-birth aggregates survive that DROP.

## Notes

- `agent-{N}.log` is the only file co-written by two processes (kernel + exec subprocess use O_APPEND atomic append, single-line JSONL < PIPE_BUF 4KB won't interleave); `enqueue=False` is deliberate (see semaphore leak above).
- Agent graph `node_enter` / `node_exit` / timeline snapshot logging in `agent/graph/_node_log.py` (agent domain) are merely consumers of this module's logger.

## Key Dependencies

- [[db.ava.okf.md]] — Postgres connection pool / `events` tables
- [[metrics.ava.okf.md]] — computes system-level metrics on top of `events`
- `shared/telemetry.py` — the unified emitter (queue + drain + batch writer)
