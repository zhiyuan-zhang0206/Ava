---
type: doc
title: Agent Messages
description: Ava-style message constructors — builds standard LangChain `HumanMessage` / `ToolMessage`, carrying Ava-specific metadata via `additional_kwargs`. The reading side uniformly classifies via `read_ava_kwargs(msg).get("ava_msg_type")` without using `isinstance` subclass checks.
tags:
- agent-core
- runtime
- agent-lifecycle
---

# Agent Messages

## What it is

Ava-style message constructors — builds standard LangChain `HumanMessage` / `ToolMessage`, carrying Ava-specific metadata via `additional_kwargs`. The reading side uniformly classifies via `read_ava_kwargs(msg).get("ava_msg_type")` without using `isinstance` subclass checks.

## Core Responsibilities

- **Message construction helpers**: `<purpose>_message()` naming convention, at minimum fills `ava_msg_type` discriminator
- **Typed metadata contract** (`base/agents/messages/kwargs.py`): `additional_kwargs` is forced to bare `dict` by LangChain, cannot use pydantic model; the type contract lives in `AvaMessageKwargs` (`TypedDict, total=False` — presence of keys depends on message kind) + `AvaMsgType` (discriminator StrEnum: `ATTACH` / `INBOUND` / `SYSTEM_NOTE` / `EXEC_OUTPUT` / `COMPACT_SUMMARY` / `COMPACT_REQUEST`) + `NoteTag`. Writing side saves `<member>.value` (plain string — bare Enum members would trigger LangGraph checkpoint msgpack custom type serialization path); reading uses `read_ava_kwargs(msg)` to re-apply `AvaMessageKwargs` type onto `additional_kwargs` (identity at runtime, no copy, no validation)
- **Attachment delivery** (`attach_message`): appends one HumanMessage with interleaved content blocks — a leading notice text block, then each file's caption line directly before its model-native media block — so every image is paired with its own label structurally. It carries `ava_msg_type="attach"` and a creation time, without a serving URL.
- **Metadata key prefix**: uniformly uses `ava_` prefix to avoid conflicts with LangChain framework metadata; third-party keys (such as `reasoning_content` written by community langchain packages) coexist in the same dict and are intentionally not part of this contract
- **`NoteTag` enum** (canonical definition in `base/agents/messages/kwargs.py`, re-exported by `agent/messages/__init__.py`): framework-injected system marker categories — `SDK_HINT`, `AGENT_REPLY`, `COMPACT_REMINDER`, `MEMORY`, `LIFECYCLE_TERMINATE`, `LIFECYCLE_RESTART`, etc.
- **`COMPACT_SUMMARY_HEADER`**: the fixed text prefix prepended to message history each time it is replaced by a compaction summary, defined in this leaf module (instead of `agent/hooks/compact.py`) so that the gateway can import it directly to classify summary messages without pulling in `agent.graph`
- **Serialization compatibility**: LangGraph PostgresSaver msgpack serialization goes through standard LangChain message classes, automatically hitting the `SAFE_MSGPACK_TYPES` whitelist
- **Append-only guard** (`agent/messages/guard.py`): the stored message list may only be wiped (compaction, crash-repair rebuild), appended to, or have its last message modified. `guarded_add_messages` checks every merge and `guarded_delta_reducer` replays stored writes through it — the `messages` channel reducers on `BaseAgentState`, so every persisted mutation funnels through one choke point

## Key Dependencies

- [[agent/docs/state.ava.okf.md]] — messages stored in BaseAgentState.messages
- [[graph.ava.okf.md]] — messages passed as history in LLM nodes
- [[message-format.ava.okf.md]] — the message types these helpers build

## Entry Points

- `base/agents/messages/kwargs.py:AvaMsgType` / `NoteTag` / `AvaMessageKwargs` / `read_ava_kwargs()` — canonical location of the type contract (`agent/messages/__init__.py` re-exports `AvaMsgType` / `NoteTag` / `read_ava_kwargs`)
- `agent/messages/__init__.py` — various `<purpose>_message()` helper functions + `COMPACT_SUMMARY_HEADER`
- `agent/messages/guard.py:guarded_add_messages` / `guarded_delta_reducer` — the append-only reducers

## Notes

- Design chooses **not to subclass** LangChain Message — distinguishes via metadata instead of `isinstance`, serialization path is simpler
- `NoteTag` is a closed set: adding a new kind requires updating UI mapping branches; unmapped tags render as a prominent "unrecognized" marker rather than silently falling back
- Writer/reader division: writer (`agent/messages/__init__.py` + `agent/graph/claim/node.py` + `agent/graph/llm/node.py`) saves `<AvaMsgType member>.value`; reader (`base/agents/history/timeline.py`, `gateway/agents/context_breakdown.py`, `agent/graph/memory_recall.py`) always gets typed view via `read_ava_kwargs()`, no longer `.get()` raw dict directly
