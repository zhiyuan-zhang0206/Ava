---
type: doc
title: Invocation Publisher Ownership
description: The invocation-owned live-event worker, bounded drain and explicit Redis failure contracts.
tags:
- base
- contract
- observability
---

# Invocation Publisher Ownership

`AgentHost._drive_turns` creates one `AgentEventPublisher` for each admitted
invocation. `invocation.driver.drive_context` enters the `TaskGroup` passed to
`publisher.start(tasks)` and closes the publisher before leaving that group.
Worker completion is joined on success, failure and cancellation; an unexpected
pipeline, command-result or pool-disconnect exception reaches the invocation as
an exception-group leaf. A lone invocation failure retains its original type
and object after the worker joins cleanly, preserving host compact/stall error
classification. An invocation failure remains visible if its drain also fails. This does not change the separate `EventBus.publish_best_effort` contract.

`emit` stays synchronous and nonblocking. One worker publishes FIFO batches of
up to 64 from a queue of 2048; overflow sheds the oldest buffered event and keeps
queue completion accounting. The publisher gives each command attempt two
seconds and close drains for up to two seconds before cancelling the worker.
Typed `AuthenticationError` and `NoPermissionError` transitions retain the
shared bounded retry. These exhausted auth failures, Redis connection failures,
timeouts and OS transport errors shed live events with the existing structured
`sse_drop` report; other `ResponseError` and `DataError` failures reach the owner;
only transport failures tear down the shared client's pool for reconnect. The
publisher never closes the `EventBus`'s shared Redis client or retries ordinary
invocation work. Durable DB state and gateway postcommit notification policy
remain with their existing owners.

Channel payloads and postcommit publishing remain documented in
[[base/events/live/docs/live/live.ava.okf.md|Live Event Channel]].
