---
type: doc
title: Redis Inbound Listener Ownership
description: Request deadlines, terminal bounded stop, known unfinished operations and late failure propagation for CLI inbound wakes.
tags:
- base
- lifecycle
- redis
---

# Redis Inbound Listener Ownership

`RedisInboundListener` serves the CLI impersonation inbox and relay. Each
consumer owns one listener lifetime and calls `stop()` in its `finally` block.
The hosted agent wake service uses a separate subscription and durable scan.

## Request and lifetime boundaries

`wait_one` serializes reads through `_wait_lock`. Open shares the request's
remaining budget; consume retains its existing two-second scheduling grace.
Request timeout or caller cancellation cancels the known operation without
waiting for its rollback. A return proves the request has returned, not that
its Redis coroutine or resources have terminated.

Wake-key GETDEL remains directly awaited by the caller with its existing
`asyncio.wait_for` timeout and cancellation behavior. It is not an additional
registered operation; cancellation-resistant GETDEL can still delay its caller.
Stop's unfinished identities describe registered work, not caller completion.

The listener registers each explicit open, eager-open, consume and handle
cleanup Task. In-flight results belong to their caller. An unclaimed result,
including completion in the same tick as caller cancellation, belongs to the
listener. Abandoned tasks remain known until completion; this is local
ownership of one consumer's work, not a global task registry or supervisor.

`stop(timeout=2.0)` stops admission, invalidates the connection generation,
cancels active operations once and waits for known work with `asyncio.wait`.
The public `timeout` argument is a caller-overridable local observation timeout,
with a two-second default; no deployment timing relationship depends on it.
Already abandoned operations and handle cleanup keep draining without a second
cancellation. Cleanup detaches the listener's handles before awaiting them;
a late open closes its own handles rather than attaching to a stopped or
replacement generation. Business task admission is checked at each open and
consume spawn. A GETDEL await that crosses stop cannot admit a later consume;
one that crosses a resource close retries against the current subscription
within the original request budget. A positive stop budget is finite; zero
skips waiting.

The return is the tuple of known unfinished operation identities. The same
live information is available through `unfinished_work`; a nonempty result
also emits a warning. Repeating stop can join remaining work and observe
failures that arrived after an earlier stop. No return claims complete resource
termination. In particular, `asyncio.run` performs its own final cancellation
and drain, which can still wait for a coroutine that resists cancellation.
Listener stop has a finite budget; process exit does not acquire that guarantee.

`close()` retains its resource-only, reconnectable contract. It is useful to
force a reconnect while the lifetime remains open; it is not the terminal
consumer exit and may wait for a handle close or the open lock.

## Failure ownership

Unknown errors from completed in-flight operations propagate directly to their
caller. Known Redis/network failures retain retry, ACL degradation and durable
SELECT recheck behavior. Redis's existing dead-transport `TypeError` recovery
applies to open/consume operations, not handle cleanup.

An unknown abandoned-operation error immediately reaches the event loop's
exception handler with its exception, Task and listener identity. The listener
retains the error for `stop` to raise with its original type. If it arrives
after stop returned, it is reported immediately and can be raised by a later
stop; it cannot be retroactively raised by the past call. Expected late
Redis/network errors do not turn successful request expiration into failure.

Closing both handles attempts the second close even if the first has an
unknown failure. An existing operation failure or cancellation remains primary;
a secondary cleanup error is reported and attached as a note. The CLI consumer
likewise keeps its original inbox/relay failure or cancellation when terminal
stop also fails. Without a primary failure, stop errors fail the consumer.

## Mechanism scope

The standard library supplies task cancellation, waiting and exception
reporting. The small listener-local result handoff exists because hard request
return and finite best-effort stop cannot await an unconditional TaskGroup
exit. If those contracts change to require cooperative complete draining, use
a lexical TaskGroup instead. A second component needing this same handoff
mechanism triggers extraction or selection of an established owner abstraction.
