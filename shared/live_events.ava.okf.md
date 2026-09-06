---
type: doc
title: Live Event Channel (`ava:events`)
description: Redis pub/sub channel carrying live agent activity to the UI. One Pydantic model per role, `role` as the Literal discriminator, `EVENT_ADAPTER` a closed discriminated union — an unknown role raises rather than degrading.
tags:
- shared
- contract
- observability
---

# Live Event Channel (`ava:events`)

## What it is

`shared/live_events.py` defines the Redis pub/sub payloads that carry **live** agent
activity to the frontend. Wire format is `{"agent_id": int, "role": str, ...}`;
every role is a `BaseModel` subclass of `_Base` (frozen — consumers are
read-only) whose `role` field is a `Literal[...]` discriminator, and `Event` /
`EVENT_ADAPTER` is the closed discriminated union the UI tailer validates
against.

The union is deliberately **not** forward-compatible: `EVENT_ADAPTER` raises
`ValidationError` on an unknown role, so adding one forces producer and consumer
to be synced in the same change rather than silently dropping frames.

It lives at the top level (not under `ava/`) so the agent can publish without
triggering SDK import side effects — same rationale as `shared/exit_codes.py`.

## Roles

All roles carry `agent_id: int`. The extra fields below are the payload.

### Kernel streaming (`agent/graph/_callbacks.py`, `_llm.py`, `_exec.py`)

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

### Turn lifecycle (`services/agent_host/host.py`, `agent/graph/_claim.py`)

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
| `agent_spawned` | new agent row INSERTed (spawn / fork); sidebar upserts | `snapshot` |
| `agent_updated` | user-visible `agents_meta` UPDATE from any writer — resurrect, respawn, row claim, claim-node transitions, terminate cleanup, `mark_agent_exited_op`, and heartbeat liveness edges entering/leaving `offline` | `snapshot` |
| `label_updated` | `agents.label` written (spawn-time generation / rename / reset) | `label` (nullable) |
| `notice_posted` | `ava.ui.notify()` row created | `notice_id`, `priority`, `title`, `task_id` (nullable) |
| `notice_resolved` | notice dismissed | `notice_id` |
| `task_created` / `task_updated` | task registry write | `task_id` |
| `page_opened` | `ava.ui.show` registered a page | `page_id`, `name`, `port`, `title`, `url` |
| `page_closed` | `ava.ui.close` | `name` |
| `cluster_update_started` | whole-cluster rollout/restart orchestration spawned; global UI takeover hint | `kind`, `origin` (`agent_id` is `0`) |

## Notes

- **Deltas are never persisted.** Granularity is too fine, and the LangGraph
  checkpointer already stores the committed step. To replay code segments after
  a UI restart, read the checkpointer — not this channel.
- Publishing goes through `shared/redis_client.py:publish_best_effort` — a
  fire-and-forget primitive that never raises, so a redis hiccup degrades the
  live UI without breaking the DB write or the agent lifecycle path that
  triggered it.
- The channel name is cluster-scoped (`ava:*`), which is also the scope of the
  per-cluster redis ACL user.

## Key Dependencies

- [[agents-contract.ava.okf.md]] — the sibling agent ↔ gateway contract; `AgentSnapshot` (`shared/agent_snapshot.py`) is the payload of `agent_spawned` / `agent_updated`
- [[gateway/routers/sse.ava.okf.md]] — the gateway leg that fans this channel out to browsers over SSE
