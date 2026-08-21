---
type: doc
title: Agent Graph (LangGraph Execution Graph)
description: Ava agent's core execution engine—a **8-Node self-looping directed graph** based on LangGraph, entirely routed via `Command(goto=)`
tags: []
---

# Agent Graph (LangGraph Execution Graph)

## What it is

Ava agent's core execution engine—a **8-Node self-looping directed graph** based on LangGraph, entirely routed via `Command(goto=)` for explicit routing. The graph compiles into a `CompiledStateGraph`; **one invocation = one turn**: the runloop (`agent/_runloop.py`) invokes the graph once per turn, and claim ends the invocation at the turn boundary (goto END with `exit_requested=False` — nothing left to do but wait) so the per-turn root span/trace closes; the runloop re-invokes on the same checkpointer thread and the fresh invocation's claim does the long wait. `exit_requested=True` (terminate/restart winner, lost lifecycle CAS) is what makes the runloop return and the process exit.

```
after_init → init_context → claim → before_llm → llm → before_exec → exec → after_exec
                  ↑           │                                                     │
                  │           └─────────────────────────────────────────────────────┘
                  │           (after_exec always returns to claim; claim decides
                  │            end-turn or continue based on halted + pending +
                  │            turn_active)
                  └── compaction routes claim → init_context to re-establish the
                      standing message head (post-compact tail parked in context_reset)
```

## Core Responsibilities

- **init_context** (`_init_context.py`): Sole owner of the standing message head (SystemMessage + the ordered context notes). Lays it down whenever `messages` is empty — an agent's first wake, and the turn after any compaction, which routes back here with the post-compact tail parked in `state.context_reset`. A pass-through otherwise.
- **claim** (`_claim.py` + `_claim_batch.py` / `_claim_routing.py` / `_claim_dispatch.py` / `_claim_decide.py`): Long-wait on Redis pub/sub for inbound messages, dispatches to different processing paths (new message / resume / terminate / restart)
- **before_llm** (hook container): Runs all registered `register_before_llm` hooks—plugins can modify state or inject extra context
- **llm** (`_llm.py`): Streaming LLM inference, supports mid-stream cancellation (cancel = discard current turn); streaming-first with one non-streaming fallback; fatal provider errors fail-fast to idle
- **before_exec** (hook container): Runs `register_before_exec` hooks—final checkpoint before tool invocation
- **exec** (`_exec.py`): Runs Python sandbox code in one disposable subprocess (SIGINT/SIGTERM → SIGKILL(-pgid) escalation)
- **after_exec** (hook container): Runs `register_after_exec` hooks—cleanup/recording after execution

## Interrupting a turn: cancel and terminate

Both are **durable inbound rows**, not a Redis control channel. `POST
/api/cancel` INSERTs `kind='cancel'`; terminate shares the same machinery.
An in-flight llm/exec node notices it via a short-cadence **DB poll of
`inbound_messages`** (`agent/graph/_interrupt.py`, `_INTERRUPT_POLL_S` = 2s —
deliberately not sharing the claim node's Redis pub/sub listener: that sharing
was the 2026-08-02 lost-wake root cause), sets `halted=True`, and routes to
claim. The two differ only in claim's routing:

| | claim routes to | Process |
|---|---|---|
| cancel | idle | stays alive, publishes `cancelled` |
| terminate | `END` | exits (unless vetoed — see below) |

A terminate **yields to a message the death decision did not see**: a chat
co-batched with a self-initiated terminate (`ava.self.terminate()` — the chat
arrived during the very turn the agent terminated in), a chat newer than an
external terminate (the latest intent wins, same recency rule as the
resurrect veto), or any pending inbound newer than the whole claimed batch (a
final queue recheck before the exit is committed). The vetoed terminate is a
consumed no-op: no marker, no `END`, never retried — the agent stays alive and
processes the message, so the last message is never swallowed by a racing
death.

Node-level discard semantics differ, and deliberately so:

- **llm_node** discards the partial generation whole — nothing was committed, so
  there is nothing worth keeping.
- **exec_node** keeps the partial stdout/stderr and appends a `[cancelled by
  user]` tool_result — the side effects already happened, so hiding the output
  would make them invisible.

Durability is what makes this correct under a race: a cancel landing *between*
actions is not lost, because the row is dispatched by the next claim pass.

## Key Dependencies

- [[state.ava.okf.md]] — State channel definitions (BaseAgentState + plugin state merging)
- [[agent/hooks/hooks.ava.okf.md]] — Hook registration/execution mechanism
- [[llm.ava.okf.md]] — LLM node's streaming inference
- [[tool-calls.ava.okf.md]] — exec node's sandbox execution
- [[system-prompt.ava.okf.md]] — LLM node's prompt assembly

## Entry Points

- `agent/graph/_build.py:build_graph()` — Assembles the 8-node topology, compiles `CompiledStateGraph`
- `agent/nodes.py` — Node name constants (`CLAIM`, `BEFORE_LLM`, `LLM`, `BEFORE_EXEC`, `EXEC`, `AFTER_EXEC`, `AFTER_INIT`, `INIT_CONTEXT`; located in `agent/` top-level, not under `agent/graph/`; `agent/graph/_nodes.py` is a compatibility re-export—see `agent/nodes.py` docstring for the hooks⇄graph circular dependency explanation)
- `agent/graph/__init__.py` — Public API re-exports

## Notes

- No LangGraph conditional edges—all routing is explicit `Command(goto=...)`, so completeness is verifiable at compile time
- The LLM node has a dedicated `RetryPolicy` (`agent/graph/_build.py:_build_llm_retry()`, parameters read from `settings.llm_retry_*`, defaults max_attempts=6 / initial=30s / max=480s / backoff=2): covers multi-provider network jitter + rate-limit bursts, and explicitly excludes `FatalLLMStreamError` / `FatalProviderError` (deterministic errors fail fast instead of exhausting retries)
- after_exec always returns to claim (not only when halted)—allows incoming user chats that arrive mid-multi-step to be claimed promptly
- Dependency injection via `Runtime[AvaContext]` (`shared/context.py`), not hardcoded in `build_graph`
