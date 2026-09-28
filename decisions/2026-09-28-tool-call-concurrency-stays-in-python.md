# Tool-call concurrency stays in Python

## Context

A model can emit several `execute_code` calls in one assistant message. Each
call needs its own result and execution boundary. Combining their source into
one script changes scope, exception behavior and call/result identity.

Running these calls concurrently would also introduce an implicit concurrency
API outside Python. Calls can update shared plugin state and trigger cancellation,
restart, termination or compaction. A parallel dispatcher would have to define
snapshot isolation, conflicting writes, partial completion and lifecycle ordering.
Those responsibilities do not belong in Ava's minimal execution core.

## Decision

The agent framework executes multiple tool calls sequentially in emitted order,
with a fresh child and a separate ToolMessage for each original call ID. It does
not concatenate their code or introduce parallel tool-call scheduling.

Parallelism remains explicit in the existing execution model:

- Tools such as understand and web fetch can parallelize internally and own their
  resource and result semantics.
- An agent can run a concurrent Python script in a background shell session.
- Python code can use `asyncio.run()` and `asyncio.gather()` for awaitable work;
  synchronous SDK calls do not become asynchronous merely by being passed to
  `gather()`.

Each tool invocation uses an ordinary graph step and returns its state delta to
LangGraph. LangGraph applies the existing channel reducers and checkpointing
before the next invocation. The exec node must not emulate this with a private
working-state snapshot, generic reducer replay or a batch state accumulator.

## Alternatives rejected

- **Parallel dispatch of sibling tool calls.** It creates a second concurrency
  model, with shared-state conflict resolution and lifecycle coordination, while
  the existing tools and Python already provide explicit parallelism.
- **Concatenate snippets into one execute_code call.** It loses result identity,
  shares Python globals and lets one exception prevent the later snippets from
  running.
- **Run a serial batch inside one graph step and accumulate plugin deltas.** It
  duplicates the state engine and assumes reducers can combine deltas separately
  from their current state. Sequential state transitions do not generally admit
  that transformation.

## Consequences

Independent sibling calls do not overlap automatically. Agents needing overlap
must express it in Python or use an internally concurrent tool. Serial sibling
calls retain independent scopes and failure results.

Progress and plugin changes commit per invocation through the existing graph.
Presentation notes and attachments may wait until all tool results have been
written to preserve provider message pairing; this is a message-ordering
concern, not a plugin-state merge protocol. Cancellation and lifecycle exits
retain the existing stop semantics, and unexecuted calls are identified explicitly.
