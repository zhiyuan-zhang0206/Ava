---
type: doc
title: Hosted Force Quiescence
description: Original hosted turn ownership spans actual continuation and disposable resource cleanup.
tags: []
---

# Hosted Force Quiescence

Force uses the existing terminate inbound's fixed target incarnation and active
pointer. Database termination is its accepted/applied decision, not process or
continuation completion. The original live host observes only after its actual
serialized turn and managed resources end. The command remains unobserved when
HTTP cancellation fails or work survives cancellation.

The scheduler owns one Task per actual turn. It captures that Task before
checking the force command, and never cancels a replacement after validation.
The host shields real awaited work, including thread-backed work, from outer
Task cancellation; accepted force remains visible to existing durable interrupt
checks. An idle force wake uses the same original-host serialized pump without
admitting another runtime.

`shared/turn_identity.py` carries a turn-local resource scope alongside identity.
Copied graph contexts share actual disposable exec domains and request evidence.
Only successful close/root/reap/reader results remove the exact entry. Formatting
an `ExecTeardownError` into a tool failure does not erase the evidence. Unknown
POSIX members are errors, not proof that the process group is empty.

A real Popen refusal before child creation, after preallocated resources close,
does not leave a fictitious live child. Reader-only cleanup failure may recover:
the existing close/root/reap results must have succeeded, and a completion task
waits for the same actual reader to exit. Exact request/domain CAS then wakes
the original scope. Other uncertain cleanup retains its task and diagnostic
evidence; a new cache, elapsed grace or retry count cannot clear it.

## Remaining boundary

On exclusive agent-host boot, an applied force left by the dead host is observed
only when no persistent `req-*.json` envelope remains for that agent. The
envelope is created before a disposable exec child and removed after close,
root reap, and reader completion, so its absence is the durable resource-free
witness. Request and Windows gate leftovers are not age-pruned; normal exact
resource settlement removes them. The database settlement re-locks the exact
target generation, owner, and command before clearing its active pointer.

This does not establish hard-host-death recovery with active exec. Any surviving
request envelope keeps recovery deferred: independent POSIX children may survive
and the parent's unreaped root pin may be lost. Host PID/birth, lease expiry, or
owner UUID alone cannot prove managed-domain completion. Persistent shell
sessions deliberately retain their separate ownership and are not disposable
exec descendants to kill wholesale.
