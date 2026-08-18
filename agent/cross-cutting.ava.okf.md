---
type: doc
title: Cross-Cutting Concerns
description: Ava's **cross-cutting concerns**—shared mechanisms that span all subsystems. These are not independent "modules", but foundational capabilities that permeate every domain like agent-runtime, infra, gateway
  and so on.
tags:
- cross-cutting
---

# Cross-Cutting Concerns

Ava's **cross-cutting concerns**—shared mechanisms that span all subsystems. These are not independent "modules", but foundational capabilities that permeate every domain like agent-runtime, infra, gateway, etc.

## Sub-concepts

- **Logging** — Structured logging system: event-driven logfmt format, unified event pipeline (emitter → `events` table), node_enter/node_exit lifecycle events
- **Sessions** — session lifecycle: naming conventions, environment variable forwarding, records + logs
- **Startup Sequence** — agent process startup sequence: schema gate → 'starting' state → heavy imports → run loop
- **Environment Variables** — Key environment variables and their propagation chain: AVA_AGENT_ID, AVA_CLUSTER_SECRET, AVA_HOME, etc.
- **Process Lifecycle** — Process lifecycle management: spawn → running → terminate → resurrect
