---
type: doc
title: IM Bridge task lifecycle and failures
description: "Service-owned IM tasks, declared network retries and strict local state restoration."
tags: []
---

# IM Bridge task lifecycle and failures

Owner: [[im_bridge]].


## Service lifecycle

The daemon's entered `asyncio.TaskGroup` owns subscription, typing, inbound
outbox replay and all three adapter pollers. Unexpected child errors fail the
daemon; supervision may restart it. The group drains children before adapters,
health and the shared database pool close. Feishu websocket callbacks create handlers in that same group; unexpected
worker exit or faults reach its owner through an instance-local Future.
`begin_shutdown()` rejects queued callbacks and ends the owner wait before group
exit; this does not prove the thread stopped. Its existing disconnect budget and
best-effort thread shutdown contract remain. Late thread outcomes after the
owner stops receiving are outside its completed lifecycle wait. The same group
owns the await task for the blocking SDK call via `asyncio.to_thread`; startup
waits for worker entry, which does not prove a websocket connection. Shutdown
cancels that wait without claiming the SDK worker physically stopped. Daemon
main retains its existing bounded cleanup and hard exit without executor join.

Teardown attempts every adapter, health server, database pool and pidfile even
after a stop fault. Unknown cleanup faults are reported with the original body
fault in a standard exception group; they do not turn failure into success.

Only explicit network failures and HTTP 429/502/503/504 retain their existing
retry budgets. Authentication, configuration, JSON/schema and programming
errors propagate; Feishu never advances its cursor or marks a failed message
seen after a fixed number of errors. Restart retains its durable replay cursor.
Absent optional inbound outbox files mean no pending messages; existing invalid
journals fail without rewriting their bytes. Current switch/filter files are
strict. The retired notice cursor alone permits the designed one-time
`LEGACY_HISTORY_UNKNOWN` cutover, retaining possibly sent old notifications.

Immediate sends suppress resend only for a declared uncertain outcome: a known
acknowledged prefix or response lost in transport. Unknown program faults remain
visible even after an external side effect; restart may redeliver in that case.
This does not promise exactly-once delivery. Native outbox receipts are unchanged.
