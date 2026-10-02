"""The event-stream reads: the unified `/api/events` query, per-agent event
history + live SSE tail, computer-use traces, class-level event resolutions,
aggregate metrics over the stream, and the Redis -> SSE bridge with its
system-broadcast endpoints.

Package door — no imports, no re-exports; callers use the modules below.
Routers mounted in `gateway/app.py`: `router`, `agent_events`,
`computer_traces`, `resolutions`, `metrics`, `system`. Modules:

  - `router.py`          — `GET /api/events`
  - `agent_events.py`    — `/api/agents/{id}/events` (+ `/stream`)
  - `computer_traces.py` — `/api/computer/traces`
  - `resolutions.py`     — `/api/event-resolutions`
  - `metrics.py`         — `/api/metrics`, `/api/metrics/agents`
  - `system.py`          — `/api/system`, `/api/agents/{id}/system`, `/api/system/all`
  - `sse.py`             — Redis pub/sub -> SSE bridge (also used by the alerts stream)
  - `schemas.py`         — the surface's wire models
"""
