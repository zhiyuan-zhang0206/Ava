"""The agent surface: `/api/agents/*` directory, spawn, lifecycle, messages and
state reads, conversation + timeline reads, the `/api/notices/*` feed, and the
chat-inbound delivery every gateway writer shares.

Package door — no imports, no re-exports; callers use the modules below. Routers
mounted in `gateway/app.py`: `router`, `lifecycle`, `state`, `timeline`,
`notices`, `conversation`. Modules:

  - `router.py`            — directory, roster, model catalog, spawn + retry-launch, label patch
  - `lifecycle.py`         — terminate / resurrect / restart / compact / cancel / impersonation expiry
  - `state.py`             — messages, system notes, pending inbound, trace messages, token usage,
                              context breakdown, completion-notice policy
  - `conversation.py`      — conversation snapshot (timeline + pending + token usage in one read)
  - `timeline.py`          — checkpoint-backed conversation timeline with compact-history paging
  - `notices.py`           — `ava.ui.notify()` notices: feeds, replies, resolution, escalations
  - `forward.py`           — cross-machine forwarding of lifecycle + spawn to the owning runner
  - `delivery.py`          — shared chat-inbound delivery (also used by tasks, uploads, work_failed,
                              the MCP endpoint and the completion digest)
  - `inbound_provenance.py` — verified request state -> inbound audit facts
  - `eval_guard.py`        — result-read boundary for evaluation-isolated callers
                              (also used by the events, memory, run-timeline and tasks reads)
  - `context_breakdown.py` — context-window breakdown view logic over checkpoint messages
  - `schemas.py`           — the surface's wire models
"""
