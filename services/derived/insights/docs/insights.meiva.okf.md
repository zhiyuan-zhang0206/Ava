---
type: doc
title: Insights service — read models over agent history
description: Gateway-side service that builds the CPU-heavy read models (today the run timeline) in its own process, on a Unix socket the gateway proxies to.
tags:
- services
---

# Insights service — read models over agent history

## What is it
A gateway-side daemon (`ServiceSpec.capabilities=GATEWAY`; no port and no fixed-table slot) that serves the read models derived from agents' checkpoints and audit record. Preparing retained history and deriving a live segment use CPU, so it lives in a process of its own: it never competes with the gateway's event loop, thread pool or GIL, and stopping it never touches the gateway.

## Shape
- `daemon.py` — pidfile, the socket and the uvicorn server. Composition root: owns one `ConfigBoot` and passes its live clock/timestamp readers to the app. Its entry captures one `LoadedCommit`, shared by logging and `CodeVersion`. One `ProcessDbGate` admits both logging and service database handles, built from this configuration owner's current dial slice. Unknown capture stays unknown; pidfile, pool, socket and bounded shutdown ordering are unchanged.
- `app.py` — `build_app(db, pool, config, *, home, catalog, timeline_inputs, default_model_reader)`: the FastAPI app and the state its routers read (`db`, `db_pool`, `config`, `catalog`, `timeline_inputs`, `default_model_reader`, `run_timeline_reader`). Its `/healthz` reports the same explicit `home` that `run` uses for the socket and pidfile. Mount new routers here.
- `config.py` — the settings slice (`InsightsConfig`).
- `run_timeline/` — the per-agent reads behind the agent view, see [[run_timeline.meiva.okf.md|run timeline reads]].

## Transport and trust
The service binds `$MEIVA_HOME/run/insights.sock` (`base.paths.insights_socket`, created under umask 0177, so mode 0600) and listens on no TCP port. It performs no authentication: the gateway admits the caller (session or bearer, pause policy, the eval-isolation check) and then forwards the request over the socket (`gateway/routers/insights.py`). Paths in the service equal the public paths, so the gateway forwards path and query unchanged. The browser never dials the service.

Typed routes are declared in both places: the service owns validation and the response model; the gateway repeats the query parameters and `response_model` so the OpenAPI contract and the generated frontend types describe them (a test compares the two). `/api/insights/{rest}` forwards the rest of that namespace without a gateway declaration.

## Failure modes
A stopped or wedged service is 502 (socket unreachable) or 504 (no reply within 120 s) on the insights routes; no other gateway route is affected. The roster's identity probe (`services/supervision/healthchecks/insights.py`) asks `GET /healthz` over the socket for this service, this home and the pidfile's pid; a wedged event loop does not answer within 3 s and the root supervisor restarts the unit. On SIGTERM the process exits without waiting for a cold build in a worker thread.

## Entry Points
The daemon uses `asyncio.Runner` so its signal path can drain async work without
waiting for `shutdown_default_executor` to finish a cold-read thread. Repeated
SIGTERM is ignored after interruption so it cannot abort that bounded drain.

- `services/derived/insights/daemon.py` — `.venv/bin/python -m services.derived.insights.daemon`
- Supervised through the roster's socket identity probe (`services/supervision/roster/__init__.py`).

Run context model fallback uses the same process configuration owner through an
explicit live default-model reader; it never reads another process settings facade.

## Daemon composition

The daemon reads its render policy and ClockConfig from its own live boot, then builds immutable handles per read. Its small pool is for short model/config row lookups; history and audit operations retain their own Database reads. The pidfile-admitted daemon replaces a predecessor's leftover socket. A restrictive umask covers the interval before chmod, and the binding owner creates the socket directory because path helpers are pure.

The process checks schema before serving. Its signal path uses an explicit Runner and hard exit rather than waiting for the default executor to finish cold history work; uvicorn reraises the signal after graceful stop. Repeated SIGTERM is ignored during the bounded exit.

```meiva-contract
id = "daemon-live-clock-owner"
kind = "invariant"
statement = "The daemon retains its supplied configuration owner: later model, timestamp and timezone edits are visible through that app's readers, while the owned pool and socket close at exit."
code = ["services/derived/insights/daemon.py:<module>"]
tests = ["services/derived/insights/tests/test_insights_service.py::test_daemon_binds_live_rendering_to_its_configuration_owner"]
```

```meiva-contract
id = "insights-service-ownership"
kind = "invariant"
statement = "Insights binds an owner-only Unix socket, creates its socket directory, serves the public route paths, and reports the explicit service home and process identity. The caller owns database handles through app shutdown."
code = ["services/derived/insights/app.py:<module>", "services/derived/insights/daemon.py:<module>"]
tests = ["services/derived/insights/tests/test_insights_service.py::test_the_socket_is_owner_only_and_replaces_a_predecessors_leftover", "services/derived/insights/tests/test_insights_service.py::test_the_binder_creates_the_directory_of_its_socket", "services/derived/insights/tests/test_insights_service.py::test_healthz_names_the_service_home_and_process"]
```
