---
type: doc
title: Agent view window reads
description: Bounded viewport requests, snapshot ownership and preparation recovery.
tags: [frontend]
---

# Agent view window reads

**Window reads.** The initial `recent_hours=24&max_units=5000` and subsequent range reads share a 35-second HTTP budget for headers and body. A retryable index-preparation response is retried at two-second intervals for at most 30 retries; permanent failures become explicit errors. Prepared histories use indexed database time ranges and only the current live segment is reconstructed. The service returns a `snapshot` identity that every subsequent window, raw-message and context read carries. A 409 refreshes the initial owner and retries against its new snapshot. Selections belong to their original snapshot and are cleared on replacement so an old index never selects a different message. Each query passes its cancellation signal: removing an agent
or leaving the page cancels its request. Timeouts use the per-agent failure and
Retry flow; selection cancellation is not a business failure. This client budget
does not impose a backend history-reconstruction deadline.

Parent: [[ui/web/src/docs/frontend-components/run-timeline/run-timeline.meiva.okf.md]].
