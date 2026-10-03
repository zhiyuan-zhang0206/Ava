---
type: doc
title: Run Timeline Reads
description: Event aggregation and independent read branches for the agent run timeline.
tags:
- gateway
---

# Run Timeline Reads

(`/api/agents/{id}/run-timeline`) — event-driven run/turn view: reads one agent's bounded history from `telemetry_events` and `audit_events` (Postgres, permanent), joins `llm_usage` to `turn_end` by span ID when available and otherwise once by completed-turn time window; execution and anomaly events use the same time-window association. Execution entries carry tool and outcome only; turn active seconds are LLM latency capped by turn duration. It returns turn rows or caller-selected time buckets with lifecycle, compact, idle, and failure markers, reports fallback and unmatched usage counts, and supports the default compact-ended or `session=current` lifecycle window. No lifecycle start within the 365-day lookback falls back to the last 24 hours. Events are paged oldest-first by offset in 1,000-row transport pages (`gateway/run_timeline/_events.py`). A request-owned worker reads the context strip concurrently with events/narrative; its request context is copied and it joins before the response or error returns.

The strip projection and its on-demand message reader are documented in
[[gateway/run_timeline/docs/run-timeline-strip.ava.okf.md]]. Read concurrency does not
change display limits, cache lifetimes, or optional-read degradation. Loki
errors remain retryable 503 responses; the strip worker joins before the
request exits even on a primary-read failure.
