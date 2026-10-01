---
type: doc
title: "Base Library"
description: "Foundational cross-process libraries: contracts, LLM providers, cluster primitives, logging, and metrics."
tags:
- base
- library
- cross-cutting
---

# Base Library

## What it is

`base/` is the foundational library layer shared by all subsystems in the Ava project. It defines cross-process data contracts, a unified abstraction for LLM providers, cluster management primitives, structured logging, and system-level metrics. According to import-linter's layer constraints, `base` is the bottom layer—agent, gateway, cli can all import it, but it does not import any upper-layer modules.

## Core responsibilities

- **LLM provider abstraction** (`base/lm/`): model construction, billing, content normalization, and stop classification for Anthropic / DeepSeek / Google / OpenAI / Xiaomi / Moonshot / Zhipu / Alibaba. Validates model/key configuration and resolves explicit temporary withdrawals at spawn boundaries.
- **Agent cross-process contract** (`base/agents/contract.py`): AgentStatus enum, exception hierarchy, wire error protocol (HTTP error transmission between gateway ↔ agent SDK).
- **Message-level contract** (`base/agents/messages/kwargs.py`): supplements the above — `AvaMsgType`/`AvaMessageKwargs` (strongly-typed view of `ava_*` metadata in `additional_kwargs`). Gateway message inserts also carry nullable server-owned audit facts — [[base/agents/messages/docs/inbound-provenance.ava.okf.md]].
- **Structured logging + metrics** (`base/log/__init__.py`, `base/telemetry/metrics/report.py`): one loguru singleton over stderr / JSONL file / the unified event pipeline, plus the metrics core (`base/telemetry/metrics/aggregate.py`) — the digest behind `/api/metrics` reads Loki aggregates via `gateway.lgtm.loki_events`. The unified emitter (`base/telemetry/emitter.py`) mirrors events to JSONL, projects compact numeric observations through `base/telemetry/metrics/observed_metrics.py`, and exports to OTLP ([[base/telemetry/otlp/docs/telemetry-otlp/telemetry-otlp.ava.okf.md|OTLP exporter]] — Loki logs + Prometheus metrics). The Postgres `events` archive was dropped with the cleanup (task #1281/#1823); the projected observations contain no log or checkpoint bodies. Projection is best-effort, deduplicated by source event identity, and transactionally updates daily sums. Upstream loss remains possible, so ingestion freshness and successful replay never certify completeness. `base/telemetry/audit_events.py` is the audit entry point.
- **Cluster management** (`base/cluster*.py`): **path-only identity** — clusters have no name, identity is the `$AVA_HOME` path (label = its basename, display only). Each home's own record (its start intent; no host-level cluster list), auth. New clusters take the fixed `DATA_PLANE_IDENTITY` (`"ava"`); existing ones keep their historical identifier, never re-derived.
- **Deploy & unit lifecycle** (`base/deploy/`): updater evidence, maintenance holds, serving state, deploy state, git provenance, the clock lattice.
- **Host substrate** (`base/host/`): OS integration (`system/`), process and file primitives, converge helpers.
- **Deploy state & liveness (R1)** — the explicit-model state (host_deploy_state posture, home lifecycle mutex, pause capability, agent alive predicate): [[base/deploy/state/docs/state.ava.okf.md]].
- **HTTP/RPC API contracts** (`base/api_contracts/`): gateway↔cli HTTP response types (`ConfigFieldView`/`ConfigSectionView` etc.), route idempotency/pause declarations (`contracts.py`) and the runner RPC envelope (`op_envelope.py`).
- **Converge status files** (`base/host/converge/_status_file.py`): shared JSON/file framing for screen capture and accessibility status; each caller owns its state and notice text.
- **Configuration & bootstrapping** ([[base/docs/configuration.ava.okf.md]]): per-domain runtime settings, settings-free field metadata, bootstrap, transport-encryption precondition, and `.env` integrity — [[base/host/env/docs/audit.ava.okf.md]].
- **Infrastructure utilities** (`base/db/__init__.py`, `base/agents/messages/chat_delivery.py`, `base/events/live/redis_client.py`, `base/pg_*.py`): Postgres/Redis client wrappers, the transaction-level message identity, the never-raise publish primitive, the streaming live events, and the long-lived pub/sub listener — [[infrastructure-utilities.ava.okf.md]].
- **Local query admission** (`base/telemetry/loki_query_budget.py`): bounded FIFO slots shared as a state machine, not as capacity. The gateway observes its four-slot budget; events maintenance owns a separate capacity-one budget.
- **Canonical Python lock** (`base/deploy/release/python_lock.py`): stdlib-only source validation used by the packaged installer and the dependency-free CI lint entry point; see [[../../cli/docs/python-install.ava.okf.md]].
- **Installation & paths** (`base/packages/extensions/install_registry.py`, `base/paths/__init__.py`, `base/deploy/release/editable_install.py`, `base/packages/plugins/enable_config.py`, `base/host/private_storage.py`): the machine-local package registry that gates the skill scanner, per-machine plugin enable state, `$AVA_HOME` path resolution, and the editable-install assertion/repair guard. POSIX structurally protects site-packages, Ava dist-info and venv bin directories except during an active cluster update; protection and write windows share one path set. Each exec spawn verifies and repairs its current interpreter without caching the small file-stat cost. Also provides owner-only storage with atomic local bytes writes for secrets and uploads — see the child nodes below.
- **Process supervision**: native service/orchestration/agent sessions, PTY-hosted agent shells, start-serving readiness gating, and daemon health/liveness — [[process-supervision.ava.okf.md]].
- **Health envelope** (`base/daemon/health_schema.py`, `base/daemon/health.py`): daemon `/healthz` and gateway `/api/health` return identity, liveness, readiness, components, and reasons; a degraded component is HTTP 503 for watchdog recovery.
- **Native process identity** (`base/native_process/`): dependency-free boot scope and birth keys are separate from process observation and signaling. Linux requires exact start ticks; reconstructed wall timestamps are diagnostic. Independent observations use native keys while retained receipt bytes remain exact. Descendant enumeration is a hint: capture validates each current ancestry edge against native generations before accepting a member. Repeated captures preserve the original receipt per birth. Linux signals retain a pidfd through identity verification and delivery; a stdlib-only libc adapter serves it, independent of optional Python build bindings; unknown identity never grants cleanup or recovery authority.
- **Transition alert policy** (`base/deploy/transition.py`): one dependency-free
  elapsed-time policy shared by machine liveness and the cluster health probe;
  a live deploy explains the bounded window, then unexplained episodes grade
  from silent to WARNING to ERROR using cluster-pinned alert thresholds.

## Key dependencies

The domain dependency map lives in [[dependencies.ava.okf.md]].

## Entry points
The base-layer public entry points: [[base/docs/entry-points.ava.okf.md]].

## Notes

- `base/host/macos_firewall.py` — declarative macOS Application Firewall manifest,
  audit, status renderer, and rootless-first reconciliation with bounded
  `sudo -n` / manual-command fallback; see
  [[base/sessions/docs/session-backend.ava.okf.md|session backend]].
- Layer constraints are enforced by `import-linter`: base < ava < agent < gateway < cli
- There is no internal layer restriction within base; services must not import agent kernel
- File line budget: soft limit 600 / hard limit 800 (enforced by lint)
