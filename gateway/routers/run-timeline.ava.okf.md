---
type: doc
title: Run Timeline Reads
description: Event aggregation and independent read branches for the agent run timeline.
tags:
- gateway
---

# Run Timeline Reads

(`/api/agents/{id}/run-timeline`) — event-driven run/turn view: reads bounded Loki history for one agent, joins `llm_usage` to `turn_end` by span ID when available and otherwise once by completed-turn time window; execution and anomaly events use the same time-window association. Execution entries carry tool and outcome only; turn active seconds are LLM latency capped by turn duration. It returns turn rows or caller-selected time buckets with lifecycle, compact, idle, and failure markers, reports fallback and unmatched usage counts, and supports the default compact-ended or `session=current` lifecycle window. Missing lifecycle history falls back to the last 24 hours. Dense Loki windows split into inclusive time slices with event-ID boundary deduplication (`_run_timeline_events`), reducing repeated prefix reads; indivisible windows and splits that repeat their parent page finish with offset paging. A request-owned worker reads the context strip concurrently with events/narrative; its request context is copied and it joins before the response or error returns.

The strip projection and its on-demand message reader are documented in
[[gateway/routers/run-timeline-strip.ava.okf.md]]. Read concurrency does not
change display limits, cache lifetimes, or optional-read degradation. Loki
errors remain retryable 503 responses; the strip worker joins before the
request exits even on a primary-read failure.
