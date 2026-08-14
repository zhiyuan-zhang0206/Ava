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
  - **hibernate flag priority**: `_hibernate_requested` is true → `"hibernate"` (see signal handler)
  - `SystemExit("signal:NAME")` → silent death caused by signal (e.g., a session kill → SIGTERM)
  - Other `SystemExit` → `"system_exit"`
  - Exception → `"exception:<TypeName>"`
  - No exception → `"normal"` (terminate inbound go to END)
- **Signal handlers** (`_install_lifecycle_signal_handlers`): SIGHUP / SIGTERM / SIGUSR1 → `SystemExit("signal:NAME")`, ensuring the finally block runs. **SIGUSR1 (hibernation swap-out) additionally synchronously sets `_hibernate_requested=True`**—because asyncio converts the handler's SystemExit into a task `CancelledError`, by the time finally runs, `sys.exc_info()` no longer sees SystemExit; the module flag is set before exception propagation, cutting through this conversion, providing a reliable channel for "this is a hibernate exit".
- **Exit notification**: `_notify_exit` → `POST /api/agents/{id}/exited` (gateway sets `terminated` + closes page); `_notify_hibernate` → `POST /api/agents/{id}/hibernating` (gateway parks `hibernating`, **retains page, does not write exit event**). `agent/loop.py:_route_process_end_notify` routes between the two based on `_exit_reason()`.

## Key Dependencies

- [[loop.ava.okf.md]] — Called in the finally block of main()
- [[db.ava.okf.md]] — Gateway manages status via agents_meta table

## Entry Points

- `agent/lifecycle.py:_exit_reason()`
- `agent/lifecycle.py:_install_lifecycle_signal_handlers()`
- `agent/lifecycle.py:_notify_exit()` / `_notify_hibernate()`
- `agent/loop.py:_route_process_end_notify()` — Routes based on reason

## Notes

- Shell sub-sessions (`ava-agent-<id>-shell-*`) are **deliberately not cleaned up** — background work (such as long-running watchers, shell sessions) should persist across terminate/restart
- The signal → SystemExit design ensures the finally block always runs, leaving no orphaned state
