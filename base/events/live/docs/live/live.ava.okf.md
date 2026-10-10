---
type: doc
title: Live Event Channel (`ava:events`)
description: Redis pub/sub channel carrying live agent activity to the UI. One Pydantic model per role, `role` as the Literal discriminator, `EVENT_ADAPTER` a closed discriminated union — an unknown role raises rather than degrading.
tags:
- base
- contract
- observability
---

# Live Event Channel (`ava:events`)

## What it is

`base/events/live/projection.py` defines the Redis pub/sub payloads that carry **live** agent
activity to the frontend. Wire format is `{"agent_id": int, "role": str, ...}`;
every role is a `BaseModel` subclass of `_Base` (frozen — consumers are
read-only) whose `role` field is a `Literal[...]` discriminator, and `Event` /
`EVENT_ADAPTER` is the closed discriminated union the UI tailer validates
against.

The union is deliberately **not** forward-compatible: `EVENT_ADAPTER` raises
`ValidationError` on an unknown role, so adding one forces producer and consumer
to be synced in the same change rather than silently dropping frames.

It lives at the top level (not under `ava/`) so the agent can publish without
triggering SDK import side effects — same rationale as `base/agents/exit_codes.py`.

## Roles

All roles carry `agent_id: int`. The extra fields below are the payload.

### Kernel streaming (`agent/graph/_callbacks.py`, `llm/node.py`, `exec/node.py`)

| Role | When | Extra fields |
|---|---|---|
| `chat_start` | model emits user-facing text for the first time this block | `item_id` |
| `chat_delta` | each text token | `item_id`, `content` |
| `code_start` | first non-empty tool-call args | `item_id` |
| `code_delta` | partial-JSON code increment | `item_id`, `content` |
| `reasoning_start` | first thinking content | `item_id` |
| `reasoning_delta` | each thinking fragment | `item_id`, `content` |
| `exec_start` | subprocess begins executing code | `item_id` |
| `exec_output_chunk` | streamed stdout/stderr increment, or an empty ~2Hz keepalive while a silent subprocess is alive (UI appends only real chunks) | `item_id`, `content`, `keepalive` (default `false`) |
| `exec_output` | subprocess done (incl. cancelled partial); upsert on the same `item_id` | `item_id`, `content` |
| `token_usage` | one LLM call finished (usage_metadata at end of stream) | `input_tokens`, `output_tokens`, `reasoning_tokens` |
| `llm_done` | LLM stream wraps up (UI reloads the timeline over the partial) | — |
| `timeline_snapshot` | server-authoritative item list; wins over streamed partials on merge | `items`, `msg_count` |

Live `item_id` is `f"{msg_idx}.{block_idx}"` — the stable key that lets the
streaming frontend and the current server timeline segment compute the *same*
id for the same logical item, so the merge is by identity, not a timestamp
heuristic. Cold-loaded pre-compact history never enters the SSE merge path; it
prefixes the local position with `s<rank>.<boundary_checkpoint_id>.` so retained
segments stay globally distinct.

### Turn lifecycle (`services/agent_runner/agent_host/host.py`, `agent/graph/claim/node.py`)

| Role | When | Extra fields |
|---|---|---|
| `compact_request` | agent called `ava.self.compact` | `content` (human-readable reason) |
| `compact_done` | compaction finished in place (agent_id unchanged) | — |
| `error` | user-visible failure (graph raised / compact failed) | `content` |
| `cancelled` | user Stop'd the current turn | — |
| `inbound_committed` | a chat inbound was envelope-wrapped into `state.messages` | `inbound_id` |

### Gateway-published

| Role | When | Extra fields |
|---|---|---|
| `inbound_arrived` | any inbound INSERT completed (UI echoes immediately) | `inbound_id`, `kind`, `source`, `content` |
| `agent_spawned` | committed creation invalidates the live roster; consumers read authoritative cards and ancestry | none |
| `agent_updated` | committed lifecycle or display-state change invalidates agent reads; no snapshot state is applied from the event | none |
| `label_updated` | `agents.label` written (spawn-time generation / rename / reset) | `label` (nullable) |
| `notice_posted` | `ava.ui.notify()` row created | `notice_id`, `priority`, `title`, `task_id` (nullable) |
| `notice_resolved` | notice dismissed | `notice_id` |
| `impersonation_changed` | committed impersonation status or reply | `agent_id`; invalidates the unified timeline |
| `task_created` / `task_updated` | task registry write | `task_id` |
| `page_opened` | `ava.ui.show` registered a page | `page_id`, `name`, `port`, `title`, `url` |
| `page_closed` | `ava.ui.close` | `name` |

## Notes

- **Deltas are never persisted.** Granularity is too fine, and the LangGraph
  checkpointer already stores the committed step. To replay code segments after
  a UI restart, read the checkpointer — not this channel.
- Publishing goes through `base/events/live/bus.py:EventBus` (`publish_best_effort`) — a
  caller-owned publish. Known Redis/network failures degrade the live UI after
  bounded recovery; unknown errors propagate to that caller. Publication after
  a durable write cannot undo its commit. Chat HTTP delivery awaits live
  `InboundArrived` publication in the current request; it owns no detached task.
  See [[base/agents/messages/docs/chat_delivery.ava.okf.md]].
- Each Redis operation owns one bounded authentication retry loop; best-effort
  publish disables command-level retry explicitly and keeps its per-attempt
  timeout. Concurrent operations have independent retry budgets.
- Each `EventBus` owns warning cadence: sync and async publishers share the
  per-channel/error-type 60-second throttle; a new bus reports failures afresh.
- The channel name is cluster-scoped (`ava:*`), which is also the scope of the
  per-cluster redis ACL user.

## Invocation publisher ownership

The invocation worker lifetime, bounded drain, failure propagation and known
Redis recovery contracts are documented in
[[base/events/live/docs/live/invocation-publisher.ava.okf.md|Invocation Publisher Ownership]].

## CLI inbound listener ownership

Request deadlines, reconnectable resource close, terminal bounded stop and late
failure propagation are owned by
[[base/events/live/docs/live/redis-listener.ava.okf.md|Redis Inbound Listener Ownership]].

## Key Dependencies

- [[agents-contract.ava.okf.md]] — the sibling agent ↔ gateway contract; lifecycle hints carry only `agent_id` and `role`, while authoritative state comes from roster/directory/detail reads.
- [[gateway/events/docs/sse.ava.okf.md]] — the gateway leg that fans this channel out to browsers over SSE

## Compaction vocabulary

`CompactionMode` (`auto`, `request`) and `CompactionStatus` (`success`,
`failure`, `replaced`) live beside the `CompactStarted`/`CompactFinished`
projection models. Producer helpers and automatic/request claim paths use
these separate owners. The event adapter converts existing wire strings to
members and rejects unknown values; role discriminators and JSON spellings
remain unchanged. These statuses describe a live run, not persisted agent
lifecycle state.
