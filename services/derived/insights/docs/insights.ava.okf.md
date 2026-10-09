---
type: doc
title: Insights service — read models over agent history
description: Gateway-side service that builds the CPU-heavy read models (today the run timeline) in its own process, on a Unix socket the gateway proxies to.
tags:
- services
---

# Insights service — read models over agent history

## What is it
A gateway-side daemon (`ServiceSpec.capabilities=_GATEWAY`; no port and no fixed-table slot) that serves the read models derived from agents' checkpoints and audit record. Rebuilding an agent's stitched history is seconds of CPU on a cold read, so it lives in a process of its own: it never competes with the gateway's event loop, thread pool or GIL, and stopping it never touches the gateway.

## Shape
- `daemon.py` — pidfile, the socket and the uvicorn server. Composition root: the only module that reads `settings` or builds the `Database`.
- `app.py` — `build_app(db, pool, config)`: the FastAPI app and the state its routers read (`db`, `db_pool`, `config`, `run_timeline_views`). Mount new routers here.
- `config.py` — the settings slice (`InsightsConfig`).
- `run_timeline/` — the per-agent reads behind the agent view, see [[run_timeline.ava.okf.md|run timeline reads]].

## Transport and trust
The service binds `$AVA_HOME/run/insights.sock` (`base.paths.insights_socket`, created under umask 0177, so mode 0600) and listens on no TCP port. It performs no authentication: the gateway admits the caller (session or bearer, pause policy, the eval-isolation check) and then forwards the request over the socket (`gateway/routers/insights.py`). Paths in the service equal the public paths, so the gateway forwards path and query unchanged. The browser never dials the service.

Typed routes are declared in both places: the service owns validation and the response model; the gateway repeats the query parameters and `response_model` so the OpenAPI contract and the generated frontend types describe them (a test compares the two). `/api/insights/{rest}` forwards the rest of that namespace without a gateway declaration.

## Failure modes
A stopped or wedged service is 502 (socket unreachable) or 504 (no reply within 120 s) on the insights routes; no other gateway route is affected. The roster's identity probe (`services/supervision/healthchecks/insights.py`) asks `GET /healthz` over the socket for this service, this home and the pidfile's pid; a wedged event loop does not answer within 3 s and the root supervisor restarts the unit. On SIGTERM the process exits without waiting for a cold build in a worker thread.

## Entry Points
- `services/derived/insights/daemon.py` — `.venv/bin/python -m services.derived.insights.daemon`
- Supervised through the roster's socket identity probe (`ops/roster/__init__.py`).
