---
type: doc
title: Log sinks
description: 'The three loguru sink types the `base/log/__init__.py` init entry points assemble — stderr, the JSONL file and the unified event pipeline — how every sink is registered with `diagnose` off, and the stdlib interception that routes `logging.getLogger` records into them.'
tags:
- base
- library
- observability
---

# Log sinks

The sinks [[log.ava.okf.md|logging]]'s `init_*` entry points assemble per
process type. `base/log/sinks.py` owns the file-sink mechanics and the stdlib
interception; the event-pipeline sink stays in `base/log/__init__.py`.

## Three sink types

- **stderr**: human-readable colored format `_HUMAN_FORMAT` (`time level a=agent_id message`), aligned with terminal scrollback habits — **not logfmt**.
- **File**: JSONL (`serialize=True` serializes the entire record as a single JSON line), rotated at 100MB / kept for 7 days (`_add_file_sink`). Without rotation `gateway.log` once grew to ~900MB.
- **Unified event pipeline**: `_postgres_sink` derives each INFO+ record to an event and enqueues it into `base/telemetry` (bounded queue + drain thread). The emitter writes each batch to the JSONL mirror + OTLP export (unified schema: `ts / trace_id / span_id / agent_id / machine / cluster / process / category / event_name / level / source / target_agent_id / attributes`), with `trace_id`/`span_id` captured from the active OTel span at enqueue time (turn_span correlation). `event` value priority: `extra["event"]` → `extra["label"]` (backwards-compatible with `[{label}]` old style) → `"log"`. `payload` = extra minus dedicated columns + `msg` (original text); when `logger.opt(exception=True)` is used, automatically merges traceback / exception_type / exception_value into payload (with a guard to distinguish real exceptions from loguru's empty RecordException `NoneType: None`). `source` = `extra["source"]` (default `"system"`).

## Registration

- **`diagnose` is always off.** Every sink goes through `add_sink`, which
  forces `diagnose=False` and refuses `diagnose=True`; the
  `lint-logger-add-diagnose` hook (`scripts/lint/diagnostics/logger_add_diagnose.py`)
  rejects any non-test `logger.add` without a literal `diagnose=False`,
  `add_sink`'s own included. loguru's default renders
  each traceback frame's local variables into the sink, which put a
  `RoleSecret` password and a password-bearing DSN into the journal and
  `release-executor.log`. `backtrace` (which frames show, never their values)
  stays at loguru's default.
- **stderr opens first.** `init_gateway_process` and `init_cli_process` open
  it before importing `base.paths`, because that import builds Settings, and
  the build logs (a runner continuing on a stale bootstrap snapshot warns).
  Importing `base/log/__init__.py` dropped loguru's default handler, so a record
  written before any sink exists is gone.

## Stdlib interception

`_StdlibInterceptHandler` routes records from stdlib `logging.getLogger(...)` into loguru sinks (many services historically used stdlib logging), otherwise their lines would only appear on stderr and not enter the event stream. Installed on the root logger; `psycopg.pool` recycling noise is gated to ERROR.
