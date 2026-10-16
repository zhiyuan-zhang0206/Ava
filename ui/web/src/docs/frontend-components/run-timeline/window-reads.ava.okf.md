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


```ava-contract
id = "snapshot-window-reads"
kind = "invariant"
statement = "The initial read requests the latest 24 hours and at most 5000 message blocks. Settled viewport reads wait 300 ms, reuse the initial snapshot, and skip windows already completely covered. Removing an agent cancels its request. Preparation retries only explicit queued/indexing/busy states; permanent failures surface. A snapshot-expired error from a window, message or context read invalidates the initial owner. Failed ranges retain initial data and manual retry targets active queries only."
code = ["ui/web/src/components/run-timeline/agent-view/use-timeline-windows.ts:useTimelineWindows"]
tests = ["ui/web/src/components/run-timeline/agent-view/use-timeline-windows.test.tsx::timeline viewport reads::starts with one bounded day and reads older windows from the same snapshot", "ui/web/src/components/run-timeline/agent-view/use-timeline-windows.test.tsx::timeline viewport reads::coalesces a drag and aborts its pending request when the agent leaves", "ui/web/src/components/run-timeline/agent-view/use-timeline-windows.test.tsx::timeline viewport reads::refreshes an expired snapshot and retries the requested time range", "ui/web/src/components/run-timeline/agent-view/use-timeline-windows.test.tsx::timeline viewport reads::keeps loaded data and exposes range failures for retry", "ui/web/src/components/run-timeline/agent-view/use-timeline-windows.test.tsx::timeline viewport reads::shows preparation and automatically retries only explicit pending states", "ui/web/src/components/run-timeline/agent-view/use-timeline-windows.test.tsx::timeline viewport reads::surfaces a failed preparation without retrying forever"]
```

```ava-contract
id = "agent-view-ownership"
kind = "invariant"
statement = "The URL preserves agents in addition order without duplicates and the last agent cannot be removed. Message indices belong to their snapshot: replacing it invalidates the selection before effects run. Block selection and arrow selection exclude each other. The context card follows the selected agent and selected message/node, otherwise the last request in view of the first loaded agent. The page owns legend highlighting so zoom and pan retain it."
code = ["ui/web/src/app/insights/run/[agents]/page.tsx:AgentViewPage"]
tests = ["ui/web/src/components/run-timeline/agent-view/agent-view.test.tsx::agent view::adds an agent by id, keeps it in the URL, and does not take an id twice", "ui/web/src/components/run-timeline/agent-view/agent-view.test.tsx::agent view::removes an agent, and never the last one", "ui/web/src/app/insights/run/[agents]/page.test.tsx::legend highlight::keeps the highlight through a zoom", "ui/web/src/app/insights/run/[agents]/page.test.tsx::context breakdown follows the point::follows a selected block, then a selected node, and is titled with its session and time", "ui/web/src/app/insights/run/[agents]/page.test.tsx::context breakdown follows the point::without a selection follows the last request inside the viewport"]
```

Parent: [[ui/web/src/docs/frontend-components/run-timeline/run-timeline.ava.okf.md]].
