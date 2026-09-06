---
type: doc
title: "Shared Libraries"
description: "Foundational cross-process libraries: contracts, LLM providers, cluster primitives, logging, and metrics."
tags:
- shared
- library
- cross-cutting
---

# Shared Libraries

## What it is

`shared/` is the foundational library layer shared by all subsystems in the Ava project. It defines cross-process data contracts, a unified abstraction for LLM providers, cluster management primitives, structured logging, and system-level metrics. According to import-linter's layer constraints, `shared` is the bottom layer—agent, gateway, cli can all import it, but it does not import any upper-layer modules.

## Core responsibilities

- **LLM provider abstraction** (`shared/lm/`): model construction, billing, content normalization, and stop classification for Anthropic / DeepSeek / Google / OpenAI / Xiaomi / Moonshot / Zhipu / Alibaba. Validates model/key configuration and resolves explicit temporary withdrawals at spawn boundaries.
- **Agent cross-process contract** (`shared/agents.py`): AgentStatus enum, exception hierarchy, wire error protocol (HTTP error transmission between gateway ↔ agent SDK).
- **Message-level contract** (`shared/message_kwargs.py`): supplements the above — `AvaMsgType`/`AvaMessageKwargs` (strongly-typed view of `ava_*` metadata in `additional_kwargs`). Gateway message inserts also carry nullable server-owned audit facts — [[inbound-provenance.ava.okf.md]].
- **Structured logging + metrics** (`shared/log.py`, `shared/metrics.py`): one loguru singleton over stderr / JSONL file / the unified event pipeline, plus the metrics core (`shared/metrics_aggregate.py`) — the digest behind `/api/metrics` reads Loki aggregates via `gateway.loki_events` since the LGTM cutover (task #1197; the PG read path was retired). The unified emitter (`shared/telemetry.py`) writes every event to the local JSONL mirror and, while enabled, dual-writes to OTLP ([[shared/telemetry-otlp/telemetry-otlp.ava.okf.md|OTLP exporter]] — Loki logs + Prometheus metrics); the PG `events` table is a read-only archive. `shared/audit_events.py` is the audit entry point.
- **Cluster management** (`shared/cluster*.py`): **path-only identity** — clusters have no name, identity is the `$AVA_HOME` path (label = its basename, display only). Home-keyed registry (`~/.ava/clusters.json`), auth, drift detection, lock, pin. New clusters take the fixed `DATA_PLANE_IDENTITY` (`"ava"`); existing ones keep their historical identifier, never re-derived.
- **Deploy state & liveness (R1)** — the explicit-model tables + lease APIs (deployment_state, host_deploy_state, Gate owner, pause capability, updater handoff, agent alive predicate, watcher registry): [[deploy-state.ava.okf.md]].
- **HTTP API contracts** (`shared/api_contracts/`): gateway↔cli HTTP response contract types (`ConfigFieldView`/`ConfigSectionView` etc.), moved down from `gateway/schemas` 2026-07-21.
- **Configuration & bootstrapping** ([[shared/configuration.ava.okf.md]]): per-domain runtime settings, settings-free field metadata, bootstrap, transport-encryption precondition, and `.env` integrity — [[env-audit.ava.okf.md]].
- **Infrastructure utilities** (`shared/db.py`, `shared/chat_delivery.py`, `shared/redis_client.py`, `shared/pg_*.py`): Postgres/Redis client wrappers, the transaction-level message identity, the never-raise publish primitive, the streaming live events, and the long-lived pub/sub listener — [[infrastructure-utilities.ava.okf.md]].
- **Local query admission** (`shared/loki_query_budget.py`): bounded FIFO slots shared as a state machine, not as capacity. The gateway observes its four-slot budget; events maintenance owns a separate capacity-one budget.
- **Canonical Python lock** (`shared/python_lock.py`): stdlib-only source validation used by the packaged installer and the dependency-free CI lint entry point; see [[../cli/python-install.ava.okf.md]].
- **Installation & paths** (`shared/install_registry.py`, `shared/paths.py`, `shared/editable_install.py`, `shared/plugins_config.py`, `shared/private_storage.py`): the machine-local package registry that gates the skill scanner, per-machine plugin enable state, `$AVA_HOME` path resolution, and the editable-install assertion/repair guard. POSIX structurally protects site-packages except during an active cluster update; each exec spawn verifies and repairs its current interpreter without caching the small file-stat cost. Also provides owner-only storage with atomic local bytes writes for secrets and uploads — see the child nodes below.
- **Process supervision** (`shared/posixproc.py`, `shared/winproc.py`, `shared/session_backend.py`, `shared/daemon_shutdown.py`, `shared/daemon_health.py`, `shared/start_serving.py`): services, orchestration sessions and agent processes are **native** sessions; agent shells run on per-session **PTY hosts** ([[shared/pty_sessions/pty_sessions.ava.okf.md]]); Windows uses winproc. Start-serving gates recovery until readiness passes. Daemon health accepts either one `Liveness` heartbeat or a worst-case `LivenessGroup` whose per-loop progress snapshots make concurrent-loop failures attributable. Launch shape, the kill contract and the SIGTERM unwind: [[shared/session-backend/session-backend.ava.okf.md|session backend]]. External coding tools add [[coding-session-owner.ava.okf.md|canonical generation ownership]].
- **Health envelope** (`shared/health_schema.py`, `shared/daemon_health.py`): daemon `/healthz` and gateway `/api/health` return identity, liveness, readiness, components, and reasons; a degraded component is HTTP 503 for watchdog recovery.
- **Transition alert policy** (`shared/transition.py`): one dependency-free
  elapsed-time policy shared by machine liveness and the cluster health probe;
  a live deploy explains the bounded window, then unexplained episodes grade
  from silent to WARNING to ERROR using cluster-pinned alert thresholds.

## Key dependencies

The domain dependency map lives in [[dependencies.ava.okf.md]].

## Entry points
The shared-layer public entry points: [[shared/entry-points.ava.okf.md]].

## Notes

- `shared/macos_firewall.py` — declarative macOS Application Firewall manifest,
  audit, status renderer, and rootless-first reconciliation with bounded
  `sudo -n` / manual-command fallback; see
  [[shared/session-backend/session-backend.ava.okf.md|session backend]].
- Layer constraints are enforced by `import-linter`: shared < ava < agent < gateway < cli
- There is no internal layer restriction within shared; services must not import agent kernel
- File line budget: soft limit 600 / hard limit 800 (enforced by lint)
