---
type: doc
title: Message Metadata Contract
description: '`base/agents/messages/kwargs.py` — the message-level contract: strongly-typed `ava_*` metadata carried inside a LangChain message''s `additional_kwargs`. A leaf both the agent and the gateway import, so writers and readers of that dict share one vocabulary without an agent↔gateway cycle.'
tags:
- base
- library
- agent-lifecycle
---

# Message Metadata Contract

## What it is

`base/agents/messages/kwargs.py` — the message-level half of the agent ↔ gateway data
contract. Where [[agents-contract.ava.okf.md]] types the HTTP wire (status enums,
exception hierarchy, error reasons), this module types what Ava writes *inside* a
message.

LangChain forces `BaseMessage.additional_kwargs` into a bare `dict`, which cannot hold a pydantic model, so a `TypedDict` (with `total=False` — each key's presence depends on message type) carries the `ava_*` metadata Ava writes into it.

## Core responsibilities

- `AvaMsgType` (StrEnum): discriminant at `additional_kwargs["ava_msg_type"]` — `ATTACH`/`INBOUND`/`SYSTEM_NOTE`/`EXEC_OUTPUT`/`COMPACT_SUMMARY`/`COMPACT_REQUEST`; readers (timeline / context breakdown / memory recall) dispatch by it. Stored as `.value` (pure string): msgpack would pickle bare Enum members as a custom type, so `AvaMessageKwargs` types the field `str`; the enum is just the write/compare vocabulary.
- `NoteTag` (StrEnum): `system_note` sub-classification via `ava_note_tag` (`sdk_hint`/`lifecycle_terminate`/`heartbeat`/`security`/… closed set); timeline passes it through as UI marker `source`—unmapped tags render as a striking "unrecognized" rather than a generic note.
- `AvaMessageKwargs` (TypedDict, total=False): shape of the `ava_*` keys — `ava_msg_type`/`ava_source`/`ava_inbound_id`/`ava_created_at`/`ava_picked_up_at` (when an injected message entered the LLM context; read time = `message_read_time()` = picked-up else created)/`ava_image_urls`/`ava_note_tag`/`ava_exec_ms`/`ava_reasoning_ms_by_block`/`ava_reasoning_ms` (legacy turn-level; new turns write per-block `_by_block`, both read for old-timeline compat)/`ava_usage` (`AvaUsage`: an agent turn's final AIMessage carries the model, token tallies — `in_total`, `out_total`, `cache_read`, `cache_write_5m`/`_1h`, `reasoning` — and the usage-time price snapshot `cost_usd`/`price_*`, or `unpriced: 1`; `base/lm/usage.py` builds the dict once from the single `quote` and spreads the same one into the `llm_usage` event and onto the message, so the two cannot differ; absent on messages predating it, whose cost is unknown, never estimated). The one key outside the `ava_` prefix is `sdk_calls`: the exec_output's runtime SDK-call tally — `agent/graph/exec/node.py` attaches it, the timeline projects it onto the `agent_code` item.
- `read_ava_kwargs(msg)` — sole type-coercion entry: `cast("AvaMessageKwargs", msg.additional_kwargs)`, zero-copy, zero validation. Writers: `agent/messages/__init__.py` (+ `claim/node.py`, `llm/node.py`); readers: `base/agents/history/timeline.py`, `base/agents/history/context_breakdown.py`, `memory_recall.py`. Lives in `base/` as a leaf both sides import without agent↔gateway cycles.

## Key dependencies

- [[agents-contract.ava.okf.md]] — the sibling contract module: the HTTP wire types the two processes exchange, where this one types the metadata inside a message
- [[agent/messages/docs/messages.ava.okf.md]] — the agent-side constructors that write these keys, and the re-export point for `AvaMsgType` / `NoteTag` / `read_ava_kwargs`

## Entry points

- `base/agents/messages/kwargs.py:read_ava_kwargs` — typed reading entry point for message `additional_kwargs`
- `base/agents/messages/kwargs.py:AvaMsgType` — the `ava_msg_type` discriminant enum
- `base/agents/messages/kwargs.py:AvaMessageKwargs` — the TypedDict shape of the `ava_*` keys

## Notes

- Non-exhaustive: third-party keys (e.g., `reasoning_content` written by `ChatMoonshot`) also share this dict, intentionally left outside the contract.

## Stored source identity

`ava_ephemeral_message_id=True` records that a checkpoint message lacked an ID
before read/replay normalization. LangGraph may synthesize a UUID for merging,
but that UUID does not identify the original persisted source. The marker stays
in reserved `additional_kwargs` through later checkpoint serialization; reads do
not rewrite historical rows. `base/agents/messages/identity.py` owns this
normalization, shared by sync/async tuple reads, history iterators, delta
seeds/writes and the guarded delta reducer. Working-copy/plugin/hook new deltas
are not normalized as legacy. LangGraph's pre-serialization writer remains the
ID generator for new message writes.

Timeline snapshots add `source_message_id`, `source_inbound_id` and
`source_block_idx`. They preserve the existing positional `item_id` and legacy
UI anchor behavior. A source message ID must be present and not ephemeral;
an inbound fallback must be an explicitly embedded positive `ava_inbound_id`,
never a positionally matched UI anchor. Block ordinals distinguish content in
one source message and survive timeline renumbering/compact history prefixes.
These coordinates qualify identity only: a live snapshot is not evidence of a
committed checkpoint. Consumers requiring durable acceptance must read committed
history and include the source agent/thread namespace in their logical key.

This foundation does not enqueue outbound messages, advance cursors, replay
legacy history or guarantee delivery. Issue #4477 remains open for transactional
outbound intent/cursor acceptance and provider uncertainty handling. No new ID
generator, timestamp/content hash identity or client outbox is introduced. The
reserved marker is not rendered as message text or sent in the supported
OpenAI/Moonshot, Anthropic and Google provider message payloads.
