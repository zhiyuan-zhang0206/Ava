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
- `gateway/schedules/runner.py` — `.venv/bin/python -m gateway.schedules.runner <id>`, the in-session schedule entrypoint the ScheduleManager launches; the module path is a cross-version launch contract, so it stays at the package root
- `scripts/entrypoints/gateway.py` — canonical launch script (per `gateway/app.py` docstring)

Parent node: [[gateway.ava.okf.md|Gateway]].
