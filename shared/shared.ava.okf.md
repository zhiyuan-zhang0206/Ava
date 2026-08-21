---
type: doc
title: "Shared Libraries"
description: "`shared/` is the foundational library layer shared by all subsystems in the Ava project. It defines cross-process data contracts, a unified abstraction for LLM providers, cluster management primitives, structured logging, and system-level metrics. According to import-linter's layer constraints, `shared` is the bottom layer—agent, gateway, cli can all import it, but it does not import any upper-layer modules."
tags:
- shared
- library
- cross-cutting
---

# Shared Libraries

## What it is

`shared/` is the foundational library layer shared by all subsystems in the Ava project. It defines cross-process data contracts, a unified abstraction for LLM providers, cluster management primitives, structured logging, and system-level metrics. According to import-linter's layer constraints, `shared` is the bottom layer—agent, gateway, cli can all import it, but it does not import any upper-layer modules.

## Core responsibilities

- **LLM provider abstraction** (`shared/lm/`): unifies model construction, token billing, inference content normalization, and stop reason classification for nine providers — Anthropic / DeepSeek / Google / OpenAI / Xiaomi / Moonshot / Zhipu / xAI / Alibaba. Also performs model/key validation at spawn boundaries.
- **Agent cross-process contract** (`shared/agents.py`): AgentStatus enum, exception hierarchy, wire error protocol (HTTP error transmission between gateway ↔ agent SDK).
- **Message-level contract** (`shared/message_kwargs.py`): supplements the above — `AvaMsgType`/`AvaMessageKwargs` (strongly-typed view of `ava_*` metadata in `additional_kwargs`).
- **Structured logging + metrics** (`shared/log.py`, `shared/metrics.py`): one loguru singleton over three sink types (stderr / JSONL file / unified event pipeline), plus the metrics core (`shared/metrics_aggregate.py`) — the digest behind `/api/metrics` reads Loki aggregates via `gateway.loki_events` since the LGTM cutover (task #1197; the PG read path was retired). The unified emitter (`shared/telemetry.py`) is the single event-write entry: it batch-writes the `events` table (the one DB copy; legacy dual-write mirrors removed 2026-08) and dual-writes each batch to OTLP ([[shared/telemetry-otlp/telemetry-otlp.ava.okf.md|OTLP exporter]] — Loki logs + Prometheus metrics, 2026-08-11 stack); `shared/audit_events.py` is the audit entry point.
- **Cluster management** (`shared/cluster*.py`): **path-only identity** — clusters have no name, identity is the `$AVA_HOME` path (label = its basename, display only). Home-keyed registry (`~/.ava/clusters.json`), auth, drift detection, lock, pin. New clusters take the fixed `DATA_PLANE_IDENTITY` (`"ava"`); existing ones keep their historical identifier, never re-derived.
- **Deploy state & liveness (R1)**: the explicit-model tables + lease APIs — the `deployment_state` singleton (cluster deploy lease + phase/kind/settle/last_outcome, [[cluster_lock.ava.okf.md|cluster_lock]]), `host_deploy_state` (per-host posture + updater lease + mirror, [[host_deploy_state.ava.okf.md|host_deploy_state]]), the agent alive predicate in `shared/db.py` (see [[../agent/agent.ava.okf.md|agent domain]]), and the watcher registry ([[watcher_registry.ava.okf.md|watcher_registry]]).
- **HTTP API contracts** (`shared/api_contracts/`): gateway↔cli HTTP response contract types (`ConfigFieldView`/`ConfigSectionView` etc.), moved down from `gateway/schemas` 2026-07-21.
- **Configuration & bootstrapping** (`shared/config/`, `shared/bootstrap.py`): runtime configuration parsing (split by owning domain into sub-models like `agent`/`daemon`/`data_plane`/`general`/`services`, aggregated into `settings`, with per-field category/scope/capability metadata), system bootstrapping.
- **Infrastructure utilities** (`shared/db.py`, `shared/redis_client.py`, `shared/pg_*.py`): Postgres/Redis client wrappers; `shared/redis_client.py:publish_best_effort`/`_sync` is the never-raise publish primitive for lifecycle events (`mark_agent_status`, agent_events, labels, ops lifecycle) — fire-and-forget, on failure it only degrades with a log, never throws upwards. The agent kernel's **streaming live events** (chat_start/chat_delta/code_*/exec_*, the live view's main traffic) go through `shared/event_publisher.py`'s `AgentEventPublisher` instead (non-blocking enqueue, since 2026-06-04 #806). `shared/redis_listener.py` is the long-lived Redis pub/sub listener with auto-reconnect/resubscribe (PG LISTEN/NOTIFY → Redis pub/sub rework) that backs the claim node's inbound idle-wait (`wait_for_inbound`); the in-turn interrupt watcher polls the DB instead of sharing this listener (see `agent/graph/_interrupt.py`).
- **Installation & paths** (`shared/install_registry.py`, `shared/paths.py`, `shared/plugins_config.py`): the machine-local package registry that gates the skill scanner, per-machine plugin enable state, and `$AVA_HOME` path resolution — see the child nodes below.
- **Process supervision** (`shared/posixproc.py`, `shared/winproc.py`, `shared/session_backend.py`, `shared/daemon_shutdown.py`): services, orchestration sessions and agent processes are **native** sessions; agent shells run on per-session **PTY hosts** ([[shared/pty_sessions/pty_sessions.ava.okf.md]]); Windows uses winproc. Launch shape, the kill contract and the SIGTERM unwind: [[shared/session-backend/session-backend.ava.okf.md|session backend]].

## Key dependencies

- [[shared/lm/lm.ava.okf.md]] — LLM provider abstraction layer, used by agent/graph/_llm.py via factory to build chat models
- [[agents-contract.ava.okf.md]] — agent ↔ gateway state/exception/wire protocol contract
- [[shared/message_kwargs.ava.okf.md]] — typed `ava_*` metadata inside a message's `additional_kwargs`
- [[log.ava.okf.md]] — structured logging, feeds the unified event emitter (`shared/telemetry.py`) which writes `events` + the `agent_events` mirror
- [[metrics.ava.okf.md]] — system-level metrics computation core
- [[db.ava.okf.md]] — shared/db.py provides database connection pool, depended on by services and gateway
- [[gateway-cli.ava.okf.md]] — gateway communicates with agent processes via the contracts in shared/agents.py
- [[shared/live_events.ava.okf.md]] — `ava:events` live pub/sub payload union
- [[shared/machine.ava.okf.md]] — machine name + capability set, `machines` table, spawn-target invariant
- [[shared/migrations.ava.okf.md]] — baseline + delta schema model, applied set, version assertion
- [[paths.ava.okf.md]] — `$AVA_HOME` layout
- [[install_registry.ava.okf.md]] — `installed.json` + the skill-scanner gate
- [[shared/plugins_config.ava.okf.md]] — per-machine plugin enable state
- [[cluster_lock.ava.okf.md]] — the cluster deploy lease
- [[host_deploy_state.ava.okf.md]] — per-host deploy posture + updater lease
- [[watcher_registry.ava.okf.md]] — the `agent_watchers` registry

## Entry points
The shared-layer public entry points: [[shared/entry-points.ava.okf.md]].

## Notes

- `shared/macos_firewall.py` — read-only audit of the macOS Application
  Firewall's per-binary allow list (needs root to mutate, which Ava does not
  have); see [[shared/session-backend/session-backend.ava.okf.md|session backend]].
- Layer constraints are enforced by `import-linter`: shared < ava < agent < gateway < cli
- There is no internal layer restriction within shared; services must not import agent kernel
- File line budget: soft limit 500 / hard limit 800 (enforced by lint)
