---
type: doc
title: Message Metadata Contract
description: '`shared/message_kwargs.py` — the message-level contract: strongly-typed `ava_*` metadata carried inside a LangChain message''s `additional_kwargs`. A leaf both the agent and the gateway import, so writers and readers of that dict share one vocabulary without an agent↔gateway cycle.'
tags:
- shared
- library
- agent-lifecycle
---

# Message Metadata Contract

## What it is

`shared/message_kwargs.py` — the message-level half of the agent ↔ gateway data
contract. Where [[agents-contract.ava.okf.md]] types the HTTP wire (status enums,
exception hierarchy, error reasons), this module types what Ava writes *inside* a
message.

LangChain forces `BaseMessage.additional_kwargs` into a bare `dict`, which cannot hold a pydantic model, so a `TypedDict` (with `total=False` — each key's presence depends on message type) carries the `ava_*` metadata Ava writes into it.

## Core responsibilities

- `AvaMsgType` (StrEnum): discriminant at `additional_kwargs["ava_msg_type"]` — `ATTACH`/`INBOUND`/`SYSTEM_NOTE`/`EXEC_OUTPUT`/`COMPACT_SUMMARY`/`COMPACT_REQUEST`; readers (timeline / context breakdown / memory recall) dispatch by it. Stored as `.value` (pure string): msgpack would pickle bare Enum members as a custom type, so `AvaMessageKwargs` types the field `str`; the enum is just the write/compare vocabulary.
- `NoteTag` (StrEnum): `system_note` sub-classification via `ava_note_tag` (`sdk_hint`/`lifecycle_terminate`/`heartbeat`/`security`/… closed set); timeline passes it through as UI marker `source`—unmapped tags render as a striking "unrecognized" rather than a generic note.
- `AvaMessageKwargs` (TypedDict, total=False): shape of the `ava_*` keys — `ava_msg_type`/`ava_source`/`ava_inbound_id`/`ava_created_at`/`ava_image_urls`/`ava_note_tag`/`ava_exit_code`/`ava_cancelled`/`ava_timed_out`/`ava_exec_ms`/`ava_reasoning_ms_by_block`/`ava_reasoning_ms` (legacy turn-level; new turns write per-block `_by_block`, both read for old-timeline compat).
- `read_ava_kwargs(msg)` — sole type-coercion entry: `cast("AvaMessageKwargs", msg.additional_kwargs)`, zero-copy, zero validation. Writers: `agent/messages.py` (+ `_claim.py`, `_llm.py`); readers: `shared/timeline.py`, `gateway/context_breakdown.py`, `_memory_recall.py`. Lives in `shared/` as a leaf both sides import without agent↔gateway cycles.

## Key dependencies

- [[agents-contract.ava.okf.md]] — the sibling contract module: the HTTP wire types the two processes exchange, where this one types the metadata inside a message
- [[agent/messages.ava.okf.md]] — the agent-side constructors that write these keys, and the re-export point for `AvaMsgType` / `NoteTag` / `read_ava_kwargs`

## Entry points

- `shared/message_kwargs.py:read_ava_kwargs` — typed reading entry point for message `additional_kwargs`
- `shared/message_kwargs.py:AvaMsgType` — the `ava_msg_type` discriminant enum
- `shared/message_kwargs.py:AvaMessageKwargs` — the TypedDict shape of the `ava_*` keys

## Notes

- Non-exhaustive: third-party keys (e.g., `reasoning_content` written by `ChatMoonshot`) also share this dict, intentionally left outside the contract.
