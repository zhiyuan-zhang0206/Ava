---
type: doc
title: Logging
description: '`base/log/__init__.py` is the single structural logging module spanning kernel / gateway / SDK subprocesses / all daemons. A global loguru logger singleton, with per-process entry `init_*` called once to bind process-level fields and assemble three sink types: stderr / JSONL file / the unified event pipeline (`base/telemetry/emitter.py`).'
tags:
- base
- library
- observability
---

# Logging

## What is it

`base/log/__init__.py` is the single structural logging **module** (not a package) spanning kernel / gateway / SDK subprocesses / all daemons. A global loguru logger singleton, with per-process entry `init_*` called once to bind process-level fields (`agent_id`) and assemble sinks. All `from base.log import logger` get the same logger that automatically carries these fields.

`agent_id` is the one field bound **deferred** rather than frozen: the module default and `init_gateway_process` bind `base/native_process/turn_identity.py:TURN_SCOPED_AGENT_ID`, which resolves per record — turn contextvar, else the `-` sentinel (an explicit `logger.bind(agent_id=N)` still wins outright). It is what lets the agent host, one process serving many agents, attribute each record to the turn that wrote it. `base/telemetry/emitter.py:emit` applies the same order, then falls back to the process-level agent id of an exec subprocess.

Every log line is also an **event** in the unified event stream (event-system design §1): the loguru side derives `(ts, agent_id, level, event, payload, source)` and enqueues into `base/telemetry` — the unified emitter — which writes the JSONL mirror, the `telemetry_events` table and the OTLP export (the legacy `agent_events` mirror was removed with the migration window). Business (audit) events are recorded in Postgres (`audit_events`, append-only) by `base/telemetry/audit_events.py` first and flow through the same emitter afterwards as a projection. Telemetry and log events are appended to `telemetry_events` (append-only, partitioned by month) by the emitter's drain thread (`base/telemetry/event_store.py`); a batch that does not land stays in the mirror, which `services/events_maintenance/telemetry_replay.py` replays.

## Core Responsibilities

### Three process entry points
- `init_subprocess_logger(agent_id)` — exec subprocess: **only** file sink, no stderr (subprocess stderr is captured by the parent and injected as exec_output fed to the LLM; framework logs on stderr would pollute the agent context). Writes `agent-{N}.log`.
- `init_gateway_process(name)` — gateway and every long-running daemon, including agent-host, ops, watchdog, labeler, memory-indexer, heartbeat and maintenance services: stderr + `<name>.log` + unified event pipeline (process=`name`, agent_id NULL on rows); each daemon has its own `<name>.log` for easier postmortem. Also freezes this process's commit — earliest shared seam, see `base/native_process/loaded_commit.py`.
- `init_cli_process(name)` — CLI verbs that bring a unit up (`cli-<verb>`): gateway's sinks, no `service_started` row.
- All are **idempotent** (`_init_done` process-level guard) — `logger.add` is not idempotent; repeated calls accumulate sinks until fd exhaustion (errno 24); watchdog reusing healthcheck every 60s would hit this, the guard blocks it.

### Three sink types
stderr, a rotated JSONL file and the unified event pipeline, and how every sink is registered: [[sinks.ava.okf.md|log sinks]].

### Two key mechanisms
- The seven-day full and 90-day rollup-source JSONL mirrors preserve Loki-stable IDs; [[services/docs/gateway_side/events_maintenance/events_maintenance.ava.okf.md|events maintenance]] replays the rollup tier.
- The emitter's bounded queue + daemon drain thread (`base/telemetry._EventPipeline`) replaces loguru's `enqueue=True`, which uses `multiprocessing.SimpleQueue` allocating POSIX named semaphores; when an agent is SIGKILLed (routine operation) they leak permanently, eventually hitting `kern.posix.sem.max`, after which new agent startups fail with errno 28. The thread queue uses no kernel resources. A sink failure is contained on the drain thread (`catch=True`); the JSONL file sinks and the emitter's own day-stamped JSONL mirror (`$AVA_HOME/logs/events-YYYYMMDD.jsonl`) serve as durable fallback. Queue loss is an error: local diagnostics and loss summaries bypass the saturated queue, and an independent metric drives the cluster alert. See [[telemetry/otlp/telemetry-otlp/export-backpressure.ava.okf.md|Queue loss]].

## Two surfaces, and where they diverge

The sinks produce two places to look — the file `$AVA_HOME/logs/<name>.log` and
the unified event stream (Loki, which the Stats Dashboard and
`GET /api/cluster/admin/events` read) — carrying the same lines. One exception:

- **the CLI** opens sinks only for `cli.main._CLI_LOG_NAMES`; `ava status`
  opens none. CLI `print()` output remains stdout/stderr and belongs to its
  launch owner's log.

**Crash diagnosability**: every daemon wraps `asyncio.run(main())` in a top-level
`except Exception` that `logger.exception(...)`s before re-raising, so a crash
leaves a traceback in the file instead of vanishing into terminal scrollback.

## Event retention and partitioning

The PG `events` table was RANGE-partitioned by month on `ts`; the
events-maintenance daemon kept the current and next month ahead of the write
frontier so nothing stranded in the DEFAULT catch-all. Retention DROP was
never enabled: the archive cleanup dropped the table whole (task
#1281/#1823). Live retention is the JSONL mirror tiers (7d full / 90d
rollup-source) plus Loki's own. The day-grain rollups
(`agent_metrics_daily` / `agent_model_tokens_daily`) keep since-birth
aggregates alive across the retirement.

## Notes

- `agent-{N}.log` is the only file co-written by several processes (the exec subprocesses of agent N use O_APPEND atomic append, single-line JSONL < PIPE_BUF 4KB won't interleave); `enqueue=False` is deliberate (see semaphore leak above).
- Agent graph `node_enter` / `node_exit` / timeline snapshot logging in `agent/graph/node_log.py` (agent domain) are merely consumers of this module's logger.

## Key Dependencies

- [[db.ava.okf.md]] — Postgres connection pool (the `events` archive is dropped)
- [[metrics.ava.okf.md]] — computes system-level metrics on top of the event stream
- `base/telemetry/emitter.py` — the unified emitter (queue + drain + batch writer)
