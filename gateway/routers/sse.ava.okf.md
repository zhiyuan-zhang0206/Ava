---
type: doc
title: SSE (Server-Sent Events)
description: Redis pub/sub → browser EventSource real-time event bridge. Two async generators (per-event event_stream + batch-throttled throttled_event_stream) drive 4 SSE endpoints.
tags: []
---

# SSE (Server-Sent Events)

Redis pub/sub → browser EventSource real-time event bridge. The core real-time communication mechanism of the Gateway—every agent output (code_delta, tool result, idle/terminate state change) is broadcast via Redis pub/sub to the `ava:events` channel, `gateway/sse.py` subscribes and forwards to the browser.

## Architecture

```
Agent process ──▶ Redis pub/sub (ava:events)
                        │
          ┌─────────────┴──────────────┐
          ▼                            ▼
  event_stream (per-event)   throttled_event_stream (batch)
          │                            │
          ▼                            ▼
   Browser EventSource          Browser EventSource
```

## Two Generators

### event_stream (per-event forwarding)
- Each Redis message is immediately forwarded as a frame `data: {event_json}\n\n`, no buffering/batching—the browser receives the same streaming granularity as the terminal UI.
- Parameters: `agent_id` (filter by this when not broadcast), `channel`, `role_filter` (`frozenset[str]`, only forward matching roles), `broadcast` (if True, no agent_id filtering).

### throttled_event_stream (optionally filtered batch throttled)
- With `agent_filter=None`, pushes all events from all agents. With a set, pushes only those agents plus system-level `agent_id=0` events. There is no role filtering; the validated event model supplies `agent_id` in the same drain pass.
- Drain Redis messages into a batch within a flush window, flush at a fixed rate: default `settings.gateway.sse_throttle_rate = 10/sec`.
- Each frame is a JSON array: `data: [{event}, {event}, ...]\n\n`.

## Endpoints (across two routers)

| Endpoint | Generator | Filtering | Defined in |
|---|---|---|---|
| `/api/agents/{id}/events/stream` | event_stream | agent_id | agent_events.py |
| `/api/agents/{id}/system` | event_stream | agent_id + SYSTEM_ROLES | system.py |
| `/api/system` | event_stream(broadcast) | GLOBAL_ROLES | system.py |
| `/api/system/all` | throttled_event_stream | optional comma-separated `?agents=`; `agent_id=0` always passes | system.py |

## Key Features

- **Two wire formats**: `event_stream` each frame is a `events.Event.model_dump_json()`; `throttled_event_stream` each frame is a JSON array of raw payloads.
- **Heartbeat**: every 15 seconds without events, sends `data: {"role":"heartbeat"}` (drives client watchdog); otherwise sends `: hb` comment (keeps TCP/proxy alive but invisible to browser `onmessage`).
- **Subscribe-before-yield**: `pubsub.subscribe()` executed before first frame; failure results in 500 (EventSource `onerror` fires, rather than a zombie state of "connected but no events").
- **Disconnect detection**: polls `request.is_disconnected()` (`AVA_SSE_DISCONNECT_POLL_SECONDS`, default 1s).

## Fault Tolerance

- Redis IO exceptions (`ConnectionError`/`TimeoutError`/`OSError`) → push an `error` event frame then exit generator; frontend shows a toast rather than just console noise.
- In finally, each cleanup step (unsubscribe / pubsub.aclose / client.aclose) is independently suppressed and logged; one failure does not swallow others.

## Entry Points

- `gateway/sse.py:event_stream()` — per-event generator
- `gateway/sse.py:throttled_event_stream()` — batch throttled generator
- `gateway/sse.py:_drain_redis_messages()` — drain batch messages within one flush window

## Notes

SSE is unidirectional push (server→client). Frontend sends messages to agent via HTTP POST `/api/agents/{id}/messages`, not through SSE.
