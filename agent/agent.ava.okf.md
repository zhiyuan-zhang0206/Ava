---
type: doc
title: Agent
description: Overview index of the Agent subsystem. Contains 25 sub-concepts.
tags:
  - agent-core
  - agent-lifecycle
  - cross-cutting
  - runtime
---

# Agent

## What it is

Overview of the Agent subsystem.

## Terminology (domain ubiquitous language)

> DDD ubiquitous language: the same word means the same thing in code / docs / PRs / conversations. These definitions
> are defined here as the authoritative terminology; check here before adding new terms.

- **Agent** — one OS process = one LangGraph `thread_id` = one conversation / task execution unit. Has a unique
  `agent_id` (int, `str(agent_id)` is the thread_id); runs the same 8-node graph (after_init→init_context→claim→before_llm→llm→before_exec→exec→after_exec self-loop).
  Agents are **equal peers**, OS-level isolated — child agents are **not** child processes of the parent, but detached,
  reparented to init as independent processes; one dying does not affect others.
- **Mode (not a framework concept)** — "one-shot vs persistent" are **not** framework-level modes, just differences in
  the **initial prompt template** used at spawn: there is no mode field in the graph. Adding a new agent type = writing a
  new initial prompt, no framework change.
- **Lifecycle verbs** (state enumeration / wire format see [[shared/agents-contract.ava.okf.md|cross-process contract]],
  inbound kind see [[agent/db/db.ava.okf.md|database layer]], implementation in `ops/agent_spawn.py` + `ops/agent_wake.py`) — the distinction is
  "whether the new process needs to be told what it went through":
  - **spawn** — create new agent, **no inbound message delivered** (from nothing, no "why was I called" issue).
  - **resurrect** — bring a `terminated` agent back (history preserved), deliver a `kind='resurrect'` marker
    telling the model "you are resurrected" rather than continuing the previous context.
  - **respawn** — restart to replace process, deliver `kind='restart_completed'`; when restarted from idle, only commit
    the marker, no need to wake the model.
  - **fork** — new agent + inherit state of a checkpoint (including history), deliver `kind='fork'` identity marker
    correcting "who I am".
  - **terminate / force-kill** — graceful exit (deliver `kind='terminate'`, graph goes to END, process exits naturally)
    or a four-step kill ladder when stuck (`force=true`, not available on `ava.self.terminate()`).
  - **heartbeat** — check-in for an idle agent; claim appends a system note unless a permanent-provider circuit breaker is open, in which case the heartbeat is consumed without growing the LLM context.

## Sub-concepts

- [[agent/agent-runtime.ava.okf.md|Agent Runtime]]
- [[agent/cross-cutting.ava.okf.md|Cross Cutting]]
- [[agent/db/db.ava.okf.md|Db]]
- [[agent/env-vars.ava.okf.md|Env Vars]]
- [[agent/infra.ava.okf.md|Infra]]
- [[agent/lease.ava.okf.md|Lease]]
- [[agent/lifecycle.ava.okf.md|Lifecycle]]
- [[agent/loop.ava.okf.md|Loop]]
- [[agent/mcp-daemon.ava.okf.md|Mcp Daemon]]
- [[agent/message-format.ava.okf.md|Message Format]]
- [[agent/messages.ava.okf.md|Messages]]
- [[agent/observe.ava.okf.md|Observe]]
- [[agent/process-lifecycle/process-lifecycle.ava.okf.md|Process Lifecycle]]
- [[agent/startup/startup.ava.okf.md|Startup]]
- [[agent/state.ava.okf.md|State]]
- [[agent/sessions.ava.okf.md|Sessions]]
- [[agent/graph/graph.ava.okf.md|Agent Graph]]
- [[agent/hooks/hooks.ava.okf.md|Hooks]]
