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

Parent node: [[gateway.ava.okf.md|Gateway]].
