---
type: doc
title: Agent Lifecycle
description: Agent process lifecycle management—runs in the `finally` block of `main()` and signal handlers to ensure the process leaves a clean trail regardless of exit reason.
tags: []
---

# Agent Lifecycle

## What it is

Agent process lifecycle management—runs in the `finally` block of `main()` and signal handlers to ensure the process leaves a clean trail regardless of exit reason.

## Core Responsibilities

- **_exit_reason()**: Derives the exit reason label from `sys.exc_info()`
  - `SystemExit("signal:NAME")` → silent death caused by signal (e.g., a session kill → SIGTERM)
  - Other `SystemExit` → `"system_exit"`
  - Exception → `"exception:<TypeName>"`
  - No exception → `"normal"` (terminate inbound go to END)
- **Signal handlers** (`_install_lifecycle_signal_handlers`): SIGHUP / SIGTERM → `SystemExit("signal:NAME")`, ensuring the finally block runs.
- **Exit notification**: `_notify_exit` → `POST /api/agents/{id}/exited` (gateway sets `terminated`, closes only agent-owned `ava.ui.show()` pages, and retains daemon-supervised `ava.ui.serve()` / `serve_markdown()` pages). `agent/loop.py:_route_process_end_notify` keeps the finally block within its statement budget.

## Key Dependencies

- [[loop.ava.okf.md]] — Called in the finally block of main()
- [[db.ava.okf.md]] — Gateway manages status via agents_meta table

## Entry Points

- `agent/lifecycle.py:_exit_reason()`
- `agent/lifecycle.py:_install_lifecycle_signal_handlers()`
- `agent/lifecycle.py:_notify_exit()`
- `agent/loop.py:_route_process_end_notify()` — Routes based on reason

## Notes

- Shell sub-sessions (`ava-agent-<id>-shell-*`) are **deliberately not cleaned up** — background work (such as long-running watchers, shell sessions) should persist across terminate/restart
- The signal → SystemExit design ensures the finally block always runs, leaving no orphaned state
