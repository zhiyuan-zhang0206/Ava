---
type: doc
title: Agent Context (DI Container)
description: Ava agent's dependency injection container. `AvaContext` is a dataclass that carries all external dependencies required by the agent runtime (DB
tags: []
---

# Agent Context (DI Container)

## What it is

Ava agent's dependency injection container. `AvaContext` is a dataclass that carries all external dependencies required by the agent runtime (DB pool and handle, event bus, LLM instance, per-turn agent configuration, etc.). It is injected into graph nodes via LangGraph's `Runtime[AvaContext]` mechanism.

## Core Responsibilities

- **Dependency aggregation (handles)**: `ops_pool` (DB connection pool), `llm` (ChatModel instance), `event_publisher`, and the cluster `db` / `bus` handles; plus `agent`, the agent's per-turn configuration (`base.agents.context.slices.AgentSlices`, resolved by the host when the turn starts). MCP daemon ownership is host-scoped, outside the turn context.
- **Decoupling graph build**: `build_graph()` does not accept these dependencies; the caller passes them via `graph.ainvoke(..., context=AvaContext(...))`
- **Cross-node access**: Node functions access dependencies via `runtime.context.X`

## Key Dependencies

- [[agent/graph/docs/graph.ava.okf.md]] — The graph's `ainvoke()` call injects it
- [[db.ava.okf.md]] — Source of ops_pool
- [[llm.ava.okf.md]] — Source of llm instance

## Entry Points

- `base/agents/context/__init__.py:AvaContext` — Dataclass definition (canonical location)

## Notes

- `base/agents/context/__init__.py` is the canonical home; the agent host builds one per turn task and the eval driver builds its own with the handles its path needs
