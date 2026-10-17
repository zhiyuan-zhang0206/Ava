---
type: doc
title: Node lifecycle events and timeline snapshots
description: Graph node enter/exit records, the stall probe, and the incremental timeline-snapshot protocol.
tags: [agent, graph]
---

# Node lifecycle events and timeline snapshots

`agent/graph/node_log.py` wraps every graph node with one `node_lifecycle`
context: `node_enter` / `node_exit` records for death observability, and the
frontend's `TimelineSnapshot` publish on enter. All outcomes log at INFO (user
ruling 2026-08-04: node transitions are lifecycle flow, never WARNING; fatal
turns surface via the runloop's ERROR crash line).

`node_enter` carries the node name and the current `msg_count`. Normal and
cancelled exits buffer per agent and flush as one `node_exit` event at the turn
boundary (claim) or at the safety cap; an exception exits immediately, with
`logger.opt(exception=True)` inside the `except` block so the record carries the
traceback while the exception is still active (the sink injects the traceback
fields). The `BaseException` catch is why `SystemExit` / `KeyboardInterrupt`
also write an exit record; every path re-raises. Every `node_exit` record names its owning
agent explicitly — the process-wide log binding is the no-agent `-` sentinel
since the explicit-ownership change — because the Inspector activity projection
keys node durations by the record's `agent_id` and drops unattributed records.

Normal and cancelled node exits accumulate per agent and flush as one aggregated node_exit event at the turn boundary; an exception exit emits immediately at INFO with its live traceback; every path re-raises. A full buffer flushes at the cap, so a skipped flush path cannot grow it without bound.

Every node_exit record carries its owning agent's id, passed explicitly at the emit site: the process-wide log binding is the no-agent sentinel, and agent-keyed consumers (the Inspector activity projection) drop records without it.

## Timeline snapshots

On enter — not exit — the context renders a `TimelineSnapshot` from the node's
in-memory `state.messages` and publishes it; the gateway forwards it and the
frontend merges by item id, keeping one future partial. Rendering from memory is
race-free where a persisted-checkpoint reader is not: LangGraph submits the
checkpoint write to a background executor and proceeds, so the reader can miss
the just-claimed inbound. The enter snapshot reflects the previous node's
committed state; the current node's streaming message stays a future partial
until the next enter replaces it by id.

Snapshots are incremental per agent: after a published snapshot covering
`messages[:cursor]`, the next enter renders only `messages[cursor:]`. The
full-window path (cursor 0 at a process's first enter, a history shrunk below
the cursor by compaction REMOVE_ALL, or an explicit `full_window=True` — the
claim node's turn-end fallback) renders the whole tail window. The cursor
advances only after a successful publish, so a render failure leaves the next
enter a full-window retry, and `msg_count` always reports the full history
length — the frontend's future-partial boundary needs it. An empty window
publishes nothing on either path (enter-frequency SSE spam; the post-compaction
empty full window used to blank the timeline); no system-prompt special-casing:
the full-window path includes it, the incremental path never contains it, and
id-replace merging keeps one copy.

Snapshots are incremental per agent: cursor 0, a shrunk history and an explicit full_window all render the full tail window, while an empty window publishes nothing on either path and still logs node_enter. The cursor advances to the full message count only after a successful publish; msg_count always reports the full history length.

## Stall probe

`_stall_dump_guard` arms a one-shot diagnostic when a node body outlasts
`agent.node_stall_dump_seconds`: faulthandler dumps every thread's frames, and a
loop timer dumps every asyncio task's full await chain — `dump_async_tasks`
walks `cr_await` / `ag_await` past the `get_stack()` frame to the leaf awaited
line, which a thread dump cannot show. It fires at the threshold and faulthandler
2s later (different threads on one stderr; together they interleave garbage),
targeting `sys.__stderr__`'s real descriptor — a process without one skips the
dump rather than crash. 0 (the production default) is a no-op, the claim node
is exempt (it parks in the idle wait for hours), and nodes run one at a time, so
the one global timer never collides.

The stall dump arms only above a positive threshold and never for an exempt node; the task dump fires at the threshold, faulthandler 2s later, and both are disarmed on the way out. Dumps target the interpreter's original stderr descriptor (a process without one skips the dump), and the task dump renders each task's full await chain down to the leaf awaited line.
