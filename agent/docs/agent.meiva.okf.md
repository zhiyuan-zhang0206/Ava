---
type: doc
title: Agent
description: Overview index of the Agent subsystem.
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
- **Lifecycle verbs** (state enumeration / wire format see [[base/docs/agents-contract.meiva.okf.md|cross-process contract]],
  inbound kind see [[agent/db/docs/db.meiva.okf.md|database layer]], implementation in `base/agents/birth/spawn.py` + `ops/agents/wake.py`) — the distinction is
  "whether the new process needs to be told what it went through":
  - **spawn** — create new agent, **no inbound message delivered** (from nothing, no "why was I called" issue).
  - **resurrect** — bring a `terminated` agent back (history preserved), deliver a `kind='resurrect'` marker
    telling the model "you are resurrected" rather than continuing the previous context. Any new message to a
    terminated agent resurrects it the same way (there is no closed state).
  - **fork** — new agent + inherit state of a checkpoint (including history), deliver `kind='fork'` identity marker
    correcting "who I am".
  - **terminate / force** — graceful exit (deliver `kind='terminate'`, graph goes to END, process exits naturally)
    or request interruption of the active hosted turn when stuck (`force=true`, not available on `meiva.self.terminate()`);
    `enqueued` confirms acceptance, not exit or completion of owned work. With `kill_all_shell_sessions`
    every shell session the agent owns on its home machine (watchers included; `meiva.ui.serve` page sessions
    excepted) is killed too, silently — right before a graceful termination applies, or at once for force (swept
    again when the host observes the force quiescent) / an already-terminated agent — so none of them can wake
    it again; the response's `shell_sessions` reports it.
  - **heartbeat** — check-in for an idle agent; claim appends a system note unless a permanent-provider circuit breaker is open, in which case the heartbeat is consumed without growing the LLM context.

## Sub-concepts

- [[agent/docs/agent-runtime/agent-runtime.meiva.okf.md|Agent Runtime]]
- [[agent/docs/cross-cutting.meiva.okf.md|Cross Cutting]]
- [[agent/db/docs/db.meiva.okf.md|Db]]
- [[agent/docs/env-vars.meiva.okf.md|Env Vars]]
- [[agent/docs/infra.meiva.okf.md|Infra]]
- [[agent/ownership/docs/ownership.meiva.okf.md|Runtime Ownership]]
- [[agent/docs/lifecycle.meiva.okf.md|Lifecycle]]
- [[agent/docs/loop.meiva.okf.md|Loop]]
- [[agent/docs/mcp-daemon.meiva.okf.md|Mcp Daemon]]
- [[agent/messages/docs/messages.meiva.okf.md|Messages]]
- [[agent/docs/observe.meiva.okf.md|Observe]]
- [[agent/docs/process-lifecycle/process-lifecycle.meiva.okf.md|Process Lifecycle]]
- [[agent/startup/docs/startup.meiva.okf.md|Startup]]
- [[agent/docs/state.meiva.okf.md|State]]
- [[agent/docs/sessions.meiva.okf.md|Sessions]]
- [[agent/graph/docs/graph.meiva.okf.md|Agent Graph]]
- [[agent/hooks/docs/hooks.meiva.okf.md|Hooks]]
