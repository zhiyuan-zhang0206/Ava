---
type: doc
title: Gateway Entry Points
description: Where the gateway process starts — FastAPI app definition, alert reconciliation, the __main__ entry, and the canonical launch script.
tags: []
---

# Gateway Entry Points

## Entry points

- `gateway/app.py` — FastAPI application definition, lifespan, middleware, route mounting
- `gateway/alert_reconciliation.py` — fail-closed Grafana active-instance reconciliation for the alert store
- `gateway/__main__.py` — `.venv/bin/python -m gateway` → uvicorn
- `scripts/start_gateway.py` — canonical launch script (per `gateway/app.py` docstring)

Parent node: [[gateway.ava.okf.md|Gateway]].
