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

- **Dependency aggregation (handles)**: `ops_pool` (DB connection pool), `llm` (ChatModel instance), `event_publisher`, and the cluster `db` / `bus` handles; plus `agent`, the agent's per-turn configuration (`base.host.env.agent_slices.AgentSlices`, resolved by the host when the turn starts). MCP daemon ownership is host-scoped, outside the turn context.
- **Identity**: `identity` (`AgentIdentity`: `agent_id`, `owns_loop`, `actor`, and an external controller's `lease`) says who the run acts as. The host sets it for a turn it serves.
- **One type in the exec child**: the exec request envelope carries `describe()` of the turn's context (identity; serializable and secret-free), and the child builds its own instance with `from_description()` and binds it for its process (`ava.sdk_surface.process_context`), where agent code reads it as `ava.context`. Everything non-persistent a process depends on belongs to this context; what must persist rides the graph state (`ava.state` / `ava.state_update`). See [decision](../../../decisions/2026-10-04-ava-state-and-context.md).
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
- The module imports no psycopg / redis / langchain at runtime (the handle types are annotation-only, `extensions` is read through `plugin_registry()`): the exec child builds this type at boot and must stay off that stack
