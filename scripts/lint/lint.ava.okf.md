---
type: doc
title: scripts/lint/ — AST Safety Lints
description: Overview of scripts/lint/, the subdirectory holding lint_async_no_sync_blocking.py (no sync-blocking calls in an async gateway/ops handler) and lint_logger_add_diagnose.py (every logger.add(...) sink must pass a literal diagnose=False). Both are AST-based, pre-commit-wired call-site checks.
tags:
- scripts
- lint
---

# scripts/lint/ — AST Safety Lints

Referenced from [[scripts/lint-scripts.ava.okf.md|lint-scripts]] (that node's
own roster; this directory got a same-PR relocation so a new entry there did
not grow `scripts/`'s frozen direct-entry budget — see
`scripts/structure/baseline.json`). Both scripts here are AST walks over one
call shape whose omission is invisible until a specific incident reproduces it.

## `lint_async_no_sync_blocking.py`

The gateway is a single event loop; one sync psycopg / subprocess / psutil
call inside an `async def` handler freezes every other request — the
2026-08-03 incident where `/api/memory/search` ran a synchronous
gemini-embedding on the loop and the gateway went unresponsive for hours.

Flags, inside an `async def` body (not a nested `def` — presumed threaded):
DB connection/cursor/execute/fetch/commit/rollback calls, known sync DB
helpers and sync ops, process/session backend calls, filesystem calls,
`shutil.`/`subprocess.`/`psutil.` module calls, `os.system`, `time.sleep`,
and any bare `*_blocking`-suffixed helper (those exist to be wrapped in
`asyncio.to_thread`). Inline opt-out: `# async-blocking-ok: <reason>`.

Scope: `gateway/` and `ops/` only — the event-loop surfaces. Wired into
pre-commit (`lint-async-no-sync-blocking`).

## `lint_logger_add_diagnose.py`

Every `logger.add(...)` sink outside test code must pass a literal
`diagnose=False`. loguru's `diagnose` defaults to True, so an unmarked sink
renders every local variable of a `logger.exception(...)` failure's frames —
DSNs, tokens, passwords — into that sink's output (an independent review
reproduced this concretely with a `psycopg.connect(...)` failure leaking its
password). Missing the kwarg, a non-`False` literal, or a value this script
cannot verify statically (a name, a `**kwargs` unpack) are all flagged,
fail-closed; no inline exemption exists. Exempt: test code (`tests/`,
`test_*.py`, `*_test.py`).

Wired into pre-commit (`lint-logger-add-diagnose`). Full rule + rationale:
the script's own module docstring.
