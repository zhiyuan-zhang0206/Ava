"""The multi-agent cluster view: `GET /api/insights/cluster/{curves,lanes,messages}`.

Package door — no imports, no re-exports; `router` is mounted in
`services/derived/insights/app.py`. Every read here is an indexed query over the event
tables; none loads a checkpoint (that is the single-agent run timeline's job, asked for
only when a lane is expanded). Modules:

  - `router.py`    — the three endpoints and their shared query parameters
  - `selection.py` — the agent set (a root and its spawn / fork lineage) and its tree order
  - `window.py`    — the time window and the bucket width that suits it
  - `curves.py`    — cluster curves per time bucket (cost, active agents, messages, queue time)
  - `lanes.py`     — per-agent lanes: understanding-tree nodes of one level and LLM activity bars
  - `messages.py`  — agent-to-agent message edges (sent, read)
  - `schemas.py`   — the wire models
"""
