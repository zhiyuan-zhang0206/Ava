---
type: doc
title: Gateway Entry Points
description: Where the gateway process starts — FastAPI app definition, the __main__ entry, and the canonical launch script.
tags: []
---

# Gateway Entry Points

## Entry points

- `gateway/app.py` — FastAPI application definition, lifespan, middleware, route mounting
- `gateway/__main__.py` — `.venv/bin/python -m gateway` → uvicorn
- `gateway/schedules/runner.py` — the schedule execution engine; the ScheduleManager launches `services/wake/schedule_manager/runner.py` to compose the SDK in an independent process
- `scripts/entrypoints/gateway.py` — canonical launch script (per `gateway/app.py` docstring)

`cluster/process_boot.py` owns one immutable first-module-load image shared by
the Python entry and direct ASGI entry. Each root creates its own `GatewayProcess`:
its `CodeVersion` and `ProcessDbGate` are shared by logging and all database
handles, never stored as module state. The Python entry lends its root to the
ASGI lifespan; a direct ASGI lifespan constructs and closes its own root.
Health, local cluster status and MCP status report that same image, including
unknown SHA, without reading a later checkout HEAD. Uvicorn reload children
capture their own generation in their new process. Startup failures still
close constructed clients; cleanup errors preserve the original primary.

Parent node: [[gateway.ava.okf.md|Gateway]].
