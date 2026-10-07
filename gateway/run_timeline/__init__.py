"""The agent run timeline: `GET /api/agents/{id}/run-timeline` and its raw-message reads.

Package door — no imports, no re-exports; `router` is mounted in
`gateway/app.py`. Modules:

  - `router.py`     — the window endpoint: understanding-tree nodes + layer-0 units
  - `messages.py`   — raw-message range reads (`/run-timeline/messages`)
  - `history.py`    — the cached stitched history with its units and usage sums
  - `_lifecycle.py` — spawn / restart / terminate markers from the audit record
  - `schemas.py`    — the wire models
"""
