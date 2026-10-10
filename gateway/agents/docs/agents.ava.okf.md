---
type: doc
title: Gateway Agent API
description: HTTP agent lifecycle, creation, control acceptance and notice surfaces, with native execution owned by the agent runtime.
tags: []
---

# Gateway Agent API

`gateway/agents/` owns agent HTTP routes, request validation, durable acceptance
and response projections. Its `history/` package serves conversation and
timeline reads. Native lifecycle execution remains with the home runner and
agent host; an HTTP acceptance does not prove that execution completed.

## Component owners

History reads fail visibly when checkpoint storage or delta reconstruction is
unavailable: `/timeline` and its `/conversation-snapshot` consumer return 503,
rather than an empty successful conversation. `/timeline/retained` starts at
the newest retained compact boundary, or an exact `checkpoint_id`; its
`before` cursor uses the existing historical item identity. It returns
`boundary_checkpoint_id`, `items` and `has_more`, without a live message count.
This is an explicit retained-history view, never a replacement for the agent's
live execution state. It does not read the live head, resume the agent, delete
history, or repair storage. Genuine empty agents and terminal pages remain
successful empty reads; failed retained reads return 503 and renderer bugs
propagate. Configured compact-history depth still bounds accessible segments.

- [[gateway/agents/docs/agents-router/agents-router.ava.okf.md]] — lifecycle, list, state and per-agent read surfaces.
- [[gateway/agents/docs/guarded-creation.ava.okf.md]] — keyed creation and original birth acceptance.
- [[gateway/agents/docs/guarded-drafts.ava.okf.md]] — keyed draft creation.
- [[gateway/agents/docs/control-acceptance.ava.okf.md]] — cancel and compact acceptance, separate from native application.
- [[gateway/agents/docs/launch-retry.ava.okf.md]] — explicit native launch retry.
- [[gateway/agents/docs/system-note.ava.okf.md]] — system-note writes.
- [[gateway/agents/docs/notice-receipts.ava.okf.md]] — notice operation receipts.
- [[gateway/agents/docs/completion-notice-digest.ava.okf.md]] — completion notice reads and digest ownership.
- [[gateway/agents/task_assignment/docs/task-assignment.ava.okf.md]] — compound task and agent acceptance with independent launch recovery.
