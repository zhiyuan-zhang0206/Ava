---
type: doc
title: Run Timeline Strip Router
description: "The raw-context strip reads of /api/agents/{id}/run-timeline — the messages field and the per-message text route (task #4023)."
tags:
- gateway
- frontend
---

# Run Timeline Strip Router

`/api/agents/{id}/run-timeline/message` + run-timeline's `messages` field — raw-context
strip reads (task #4023): per-message kind/parts/character-width geometry from the console
timeline's own `build_timeline_items` projection. The route serves one message's text on
demand (clipped unless `full=true`).

The list read takes an optional `messages_max` per-read cap, clamped to the
`display.run_timeline_messages_max` ceiling (never raised through it); the compare view
asks for a smaller strip budget (P4-4, task #4023). The strip read itself is not gated on
`level` — a bucket response carries the strip like a turn response.
