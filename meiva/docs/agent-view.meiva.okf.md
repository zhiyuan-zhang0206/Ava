---
type: doc
title: Agent LLM View — What the Agent Sees from the LLM Perspective
description: This is the agent's "view" at runtime — from the LLM's perspective, what the agent can perceive and what it can do. Parallel to agent-runtime (the implementation mechanism at runtime), but focused on
  the agent's subjective experience rather than implementation details.
tags:
- agent-view
- agent-lifecycle
---

# Agent LLM View — What the Agent Sees from the LLM Perspective

## What it is

This is the agent's "view" at runtime — from the LLM's perspective, what the agent can perceive and what it can do. Parallel to agent-runtime (the implementation mechanism at runtime), but focused on the agent's subjective experience rather than implementation details.

## Sub-concepts

### SDK Surface
- [[sdk-surface.meiva.okf.md]] — **index**, pointing to the following modules
- [[files.meiva.okf.md]] — file system operations
- [[shell.meiva.okf.md]] — shell commands and persistent sessions
- [[meiva/agents/docs/agents.meiva.okf.md]] — agent interop
- [[meiva_builtins/plugins/meiva_fleet/docs/tasks/tasks.meiva.okf.md|Tasks]] — task registry `meiva.tasks` (injected by meiva_fleet plugin)
- [[meiva/mcps/docs/mcps.meiva.okf.md]] — MCP tool servers
- [[meiva/web/docs/web.meiva.okf.md]] — network access
- [[meiva/understand/docs/understand.meiva.okf.md]] — multimodal understanding primitives
- [[meiva/watcher/docs/watcher.meiva.okf.md|watcher]] — background watchers
- [[meiva/self/docs/self.meiva.okf.md|self]] — agent self
- [[ui.meiva.okf.md]] — user interface
- [[meiva_builtins/plugins/meiva_memory/docs/memory-api.meiva.okf.md]] — long-term memory pool
- [[meiva/skills/docs/skills.meiva.okf.md]] — skill registry

### System Prompt & Messages
- [[system-prompt.meiva.okf.md]] — how the system prompt is built
- [[message-format.meiva.okf.md]] — message formats sent and received by agents

### Execution Environment
- [[context-window.meiva.okf.md]] — context management + compaction
- [[agent/graph/exec/docs/exec.meiva.okf.md]] — code execution sandbox

## Relationship to Other Domains
- [[agent-runtime.meiva.okf.md]] — runtime implementation (parallel perspective)
- [[cross-cutting.meiva.okf.md]] — cross-cutting concerns (environment variables, logging)
- [[meiva_builtins/docs/extensions.meiva.okf.md]] — extension system (plugin, skill)
