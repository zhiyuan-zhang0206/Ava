"""The event-driven agent run timeline: `GET /api/agents/{id}/run-timeline`
and its raw-context strip reads.

Package door — no imports, no re-exports; `router` is mounted in
`gateway/app.py`. Modules:

  - `router.py`  — the run-timeline endpoint + turn aggregation
  - `strip.py`   — raw-context strip reads (`/run-timeline/message` + the `messages` field)
  - `_events.py` — complete timeline event reads without refetching the window prefix
  - `schemas.py` — the timeline wire models
"""
