"""claim node: long await + dispatch by inbound kind.

Package door — no imports, no re-exports; callers use `agent.graph.claim.node`
(re-exported as `agent.graph.claim_node` via the parent package's lazy
`__getattr__`). Modules:

  - `node.py`      — claim node: pipeline orchestrator (long await + dispatch by inbound kind)
  - `_batch.py`    — claim batch acquisition: idle wait loop, trim, chat deferral
  - `_routing.py`  — claim lifecycle routing: ClaimGoto + batch winner resolution
  - `_dispatch.py` — claim per-kind dispatch: batch state, markers, handlers
  - `_decide.py`   — claim post-dispatch decision → single Command
  - `_present.py`  — claim display: SSE publishing for the frontend timeline
"""
