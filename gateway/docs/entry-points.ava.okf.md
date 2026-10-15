---
type: doc
title: Gateway Entry Points
description: Where the gateway process starts — FastAPI app definition, the __main__ entry, and the canonical launch script.
tags: []
---

# Gateway Entry Points

## Entry points

- `gateway/app.py` — FastAPI application factories (`create_app`, `create_app_from_environment`), cold `openapi_schema()`, lifespan, middleware, route mounting
- `gateway/http/config.py` — frozen `GatewayConfig` snapshot (`gateway_config_from_boot`) shared across app assembly, auth, CORS, memory reads, and lifespan
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

The snapshot's required `memory_root` supplies search, graph, note, pool and refresh
routes. The composition boundary resolves the existing `base.paths.gateway_memory_dir`
from the current process home and the startup config's machine roles: a combined
gateway/runner uses `$AVA_HOME/gateway/memory`, while a gateway-only unit uses
`$AVA_HOME/memory`. `ConfigBoot` owns the role values, not the process home.
These routes keep the captured path instead of resolving the ambient home
again per request. Explicit app builders can supply different roots without
patching the path resolver; each app also owns its graph cache. Cold OpenAPI
assembly supplies a non-existent path without resolving the runtime home.

Refresh passes that checkout to `memory_repo.pull_main`, with keep-local and
track-branch read from the existing process ConfigBoot for each request. The
indexer and memory checkout initialization retain their existing composition.
See [[gateway/routers/docs/memory.ava.okf.md|memory route contracts]].

Parent node: [[gateway.ava.okf.md|Gateway]].

The route-contract test recursively follows the current FastAPI include
wrappers' `effective_candidates` to leaf routes and reads methods from
`original_route`. Inspecting only the wrapper can omit routes from the contract
inventory; this describes the integrated route representation, not a promise
about every future framework version.
