---
type: doc
title: Interrupt ownership
description: Explicit invocation and service ownership of interrupt watcher results.
tags: []
---

# Interrupt ownership

A database-backed `subscribe_interrupt` requires explicit `HostedTurnResources`
linked to the original `HostedServiceResources`. The four production consumers
(exec, model stream, graph compaction and hosted compaction) pass the invocation's
actual context scope. A direct graph embedder supplies that scope and joins its
service before closing the interrupt pool; no process-global owner is inferred.

Subscription exit sets stop and cancels the actual watcher, then waits at most
five seconds. Cancellation wins a same-tick race against a model result. A poll
that returns after stop cannot set the old event. In-flight unknown failures
reach the caller, preserving a primary failure and cleanup secondary together.
After the finite return, the original service retains the late watcher result:
unknown failures are visible immediately and raised by service stop/join, while
other agents continue. Service join shares the host's existing unwind deadline;
uncooperative work retains actual clients/pools until its existing hard exit.
