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
- **Typed metadata contract** (`shared/message_kwargs.py`): `additional_kwargs` is forced to bare `dict` by LangChain, cannot use pydantic model; the type contract lives in `AvaMessageKwargs` (`TypedDict, total=False` — presence of keys depends on message kind) + `AvaMsgType` (discriminator StrEnum: `ATTACH` / `INBOUND` / `SYSTEM_NOTE` / `EXEC_OUTPUT` / `COMPACT_SUMMARY` / `COMPACT_REQUEST`) + `NoteTag`. Writing side saves `<member>.value` (plain string — bare Enum members would trigger LangGraph checkpoint msgpack custom type serialization path); reading uses `read_ava_kwargs(msg)` to re-apply `AvaMessageKwargs` type onto `additional_kwargs` (identity at runtime, no copy, no validation)
- **Attachment delivery** (`attach_message`): appends one HumanMessage with interleaved content blocks — a leading notice text block, then each file's caption line directly before its model-native media block — so every image is paired with its own label structurally. It carries `ava_msg_type="attach"` and a creation time, without a serving URL.
- **Metadata key prefix**: uniformly uses `ava_` prefix to avoid conflicts with LangChain framework metadata; third-party keys (such as `reasoning_content` written by community langchain packages) coexist in the same dict and are intentionally not part of this contract
- **`NoteTag` enum** (canonical definition in `shared/message_kwargs.py`, re-exported by `agent/messages.py`): framework-injected system marker categories — `SDK_HINT`, `AGENT_REPLY`, `COMPACT_REMINDER`, `MEMORY`, `LIFECYCLE_TERMINATE`, `LIFECYCLE_RESTART`, etc.
- **`COMPACT_SUMMARY_HEADER`**: the fixed text prefix prepended to message history each time it is replaced by a compaction summary, defined in this leaf module (instead of `agent/hooks/compact.py`) so that the gateway can import it directly to classify summary messages without pulling in `agent.graph`
- **Serialization compatibility**: LangGraph PostgresSaver msgpack serialization goes through standard LangChain message classes, automatically hitting the `SAFE_MSGPACK_TYPES` whitelist

## Key Dependencies

- [[state.ava.okf.md]] — messages stored in BaseAgentState.messages
- [[graph.ava.okf.md]] — messages passed as history in LLM nodes

## Entry Points

- `shared/message_kwargs.py:AvaMsgType` / `NoteTag` / `AvaMessageKwargs` / `read_ava_kwargs()` — canonical location of the type contract (`agent/messages.py` re-exports `AvaMsgType` / `NoteTag` / `read_ava_kwargs`)
- `agent/messages.py` — various `<purpose>_message()` helper functions + `COMPACT_SUMMARY_HEADER`

## Notes

- Design chooses **not to subclass** LangChain Message — distinguishes via metadata instead of `isinstance`, serialization path is simpler
- `NoteTag` is a closed set: adding a new kind requires updating UI mapping branches; unmapped tags render as a prominent "unrecognized" marker rather than silently falling back
- Writer/reader division: writer (`agent/messages.py` + `agent/graph/_claim.py` + `_llm.py`) saves `<AvaMsgType member>.value`; reader (`shared/timeline.py`, `gateway/context_breakdown.py`, `agent/graph/_memory_recall.py`) always gets typed view via `read_ava_kwargs()`, no longer `.get()` raw dict directly
