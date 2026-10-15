---
type: doc
title: Gateway Entry Points
description: Where the gateway process starts — FastAPI app definition, the __main__ entry, and the canonical launch script.
tags: []
---

# Gateway Entry Points

## Entry points

- `gateway/app.py` — FastAPI application factories (`create_app`, `create_app_from_environment`), cold `openapi_schema()`, lifespan, middleware, route mounting
- `gateway/http/config.py` — frozen `GatewayConfig` snapshot (`gateway_config_from_boot`) shared across app assembly, auth, CORS, and lifespan
- `gateway/__main__.py` — `.venv/bin/python -m gateway` → uvicorn
- `services/wake/schedule_manager/engine.py` — the schedule execution engine; the ScheduleManager launches `services/wake/schedule_manager/runner.py` to compose the SDK in an independent process
- `scripts/entrypoints/gateway.py` — canonical launch script (per `gateway/app.py` docstring)

`cluster/process_boot.py` owns one immutable first-module-load image shared by
the Python entry and the ASGI factory entry (`gateway.app:create_app_from_environment`).
Each root creates its own `GatewayProcess`: its `CodeVersion` and `ProcessDbGate`
are shared by logging and all database handles, never stored as module state.
The Python entry lends its root to `create_app(..., process_ownership="borrowed")`;
the ASGI factory entry passes `process_ownership="owned"` so the lifespan closes its root.
Health, local cluster status and MCP status report that same image, including
unknown SHA, without reading a later checkout HEAD. Uvicorn reload children
capture their own generation in their new process. Startup failures still
close constructed clients; cleanup errors preserve the original primary.

Parent node: [[gateway.ava.okf.md|Gateway]].
