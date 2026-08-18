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
- [[sdk-surface.ava.okf.md]] — **index**, pointing to the following modules
- [[files.ava.okf.md]] — file system operations
- [[shell.ava.okf.md]] — shell commands and persistent sessions
- [[agents.ava.okf.md]] — agent interop
- [[ava_builtins/plugins/ava_fleet/tasks/tasks.ava.okf.md|Tasks]] — task registry `ava.tasks` (injected by ava_fleet plugin)
- [[presets.ava.okf.md]] — configuration presets
- [[ava/mcps.ava.okf.md]] — MCP tool servers
- [[web.ava.okf.md]] — network access
- [[understand.ava.okf.md]] — multimodal understanding primitives
- [[watcher.ava.okf.md]] — background watchers
- [[self.ava.okf.md]] — agent self
- [[ui.ava.okf.md]] — user interface
- [[ava_builtins/plugins/ava_memory/memory-api.ava.okf.md]] — long-term memory pool
- [[ava/skills.ava.okf.md]] — skill registry

### System Prompt & Messages
- [[system-prompt.ava.okf.md]] — how the system prompt is built
- [[message-format.ava.okf.md]] — message formats sent and received by agents

### Execution Environment
- [[context-window.ava.okf.md]] — context management + compaction
- [[tool-exec.ava.okf.md]] — code execution sandbox

## Relationship to Other Domains
- [[agent-runtime.ava.okf.md]] — runtime implementation (parallel perspective)
- [[cross-cutting.ava.okf.md]] — cross-cutting concerns (environment variables, logging)
- [[extensions.ava.okf.md]] — extension system (plugin, skill)
