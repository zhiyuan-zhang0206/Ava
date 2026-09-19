---
type: doc
title: Agent Context (DI Container)
description: Ava agent's dependency injection container. `AvaContext` is a dataclass that carries all external dependencies required by the agent runtime (DB
tags: []
---

# Agent Context (DI Container)

## What it is

Ava agent's dependency injection container. `AvaContext` is a dataclass that carries all external dependencies required by the agent runtime (DB connection pool, Redis client, LLM instance, inbound listener, etc.). It is injected into graph nodes via LangGraph's `Runtime[AvaContext]` mechanism.

## Core Responsibilities

- **Dependency aggregation (handles)**: `ops_pool` (DB connection pool), `inbound_listener` (dedicated Redis pub/sub connection), `llm` (ChatModel instance), `redis_sync`, `event_publisher`; plus configuration snapshots `db_url` / `redis_url` / `events_channel` / `ava_home`. MCP daemon ownership is host-scoped, outside the turn context.
- **Decoupling graph build**: `build_graph()` does not accept these dependencies; the caller passes them via `graph.ainvoke(..., context=AvaContext(...))`
- **Cross-node access**: Node functions access dependencies via `runtime.context.X`

## Key Dependencies

- [[agent/graph/graph.ava.okf.md]] — The graph's `ainvoke()` call injects it
- [[db.ava.okf.md]] — Source of ops_pool
- [[llm.ava.okf.md]] — Source of llm instance

## Entry Points

- `shared/context.py:AvaContext` — Dataclass definition (canonical location)

## Notes

- `shared/context.py` is the canonical home: non-graph entry points (gateway lifespan, daemon, CLI, eval driver) also build AvaContext
