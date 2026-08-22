---
type: doc
title: Agent Runtime
description: Ava Agent's core runtime—a self-looping execution graph based on LangGraph that drives the complete lifecycle of a single agent. Each OS process is permanently bound to one
tags: []
---

# Agent Runtime

## What it is

Ava Agent's core runtime—a self-looping execution graph based on LangGraph that drives the complete lifecycle of a single agent. Each OS process is permanently bound to one `agent_id`, launched via `agent/loop.py` and then `graph.ainvoke()` runs in an infinite loop until a `terminate` inbound message is received.

The architecture is an **8-node self-looping topology**: `after_init → init_context → claim → before_llm → llm → before_exec → exec → after_exec`, always returning to `claim` (`claim` routes to `init_context` on compaction), all routed via `Command(goto=)`. Four hook containers (`after_init / before_llm / before_exec / after_exec`) between nodes expose extension points for plugins.

## Core Responsibilities

- **Main loop** (`agent/loop.py`): Process entry point, parses `--agent-id`, initializes DB/Redis/LLM, starts the LangGraph graph loop
- **Inbound listening** (`agent/graph/_claim.py`): Waits for messages via Redis pub/sub, dispatches to corresponding processing paths
- **LLM invocation** (`agent/graph/_llm.py`): Streaming LLM inference with retry strategy and cancellation support
- **Code execution** (`agent/graph/_exec.py`): Runs `execute_code` in one disposable subprocess; every exit closes its owned domain (POSIX process group / Windows Job Object), reaps the direct child, and boundedly joins the output reader before cleanup
- **State management** (`agent/state.py`): LangGraph State, supports plugins registering their own BaseModel state blocks
- **Message construction** (`agent/messages.py`): Ava-style HumanMessage/ToolMessage with metadata classification

## Key Dependencies

- [[graph.ava.okf.md]] — LangGraph execution graph definition
- [[state.ava.okf.md]] — State channels and plugin state registration
- [[messages.ava.okf.md]] — Message metadata and classification
- [[llm.ava.okf.md]] — LLM interface and retry strategy
- [[tool-calls.ava.okf.md]] — Code execution sandbox
- [[system-prompt.ava.okf.md]] — System prompt assembly
- [[lifecycle.ava.okf.md]] — Process lifecycle
- [[hooks.ava.okf.md]] — Graph-edge hook subsystem
- [[context.ava.okf.md]] — Dependency injection container

## Entry Points

- `agent/loop.py:main()` — Process entry, never returns (infinite loop until terminate)
- `agent/__main__.py` — `.venv/bin/python -m agent --agent-id N`
- `agent/_starting.py` — Early startup state declaration (runs before heavy imports)

## Notes

- Agent processes are created via the gateway's `POST /api/agents` and are never started directly
- Shell sub-sessions are **not cleaned up** on process exit — background work can persist across terminate/restart
- All inter-node routing uses `Command(goto=...)`, no conditional edges—cleaner control flow
