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

The host explicitly supplies the original `RuntimeIncarnation`, current
`NativeWorkTarget`, and `HostedTurnResources` through `AvaContext`. The scope in
`base/native_process/turn_identity.py` belongs to the actual turn Task, while
the reusable model cache retains no scope. Copied graph contexts share actual
disposable exec owners, request evidence, and the original admission reference.
The legacy scope holds the actual `DomainCloseOwner`, including its retained
root/close/reap/reader Tasks, rather than only the native process handle.
Native close, reap, and the output tail retain their individual 5-second
budgets; aggregate observation uses their 15-second total. Native close/reap
Tasks are not cancelled to satisfy the observation deadline. The same owner
and its original unfinished Tasks stay in the service completion span until
they actually finish.
Managed execution retains registration and exact close-receipt Tasks on the
same `_OwnedRun`; cancellation observes them only until the original exec bound.
An unfinished registration crosses that boundary with its original scope and
Task identity, and its actual eventual outcome is consumed by the existing
service. Unknown late errors remain visible without cancelling other turns.
Only successful close/root/reap/reader results remove the exact entry. Formatting
an `ExecTeardownError` into a tool failure does not erase the evidence. Unknown
POSIX members are errors, not proof that the process group is empty.

A real Popen refusal before child creation, after preallocated resources close,
does not leave a fictitious live child. Reader-only cleanup failure may recover:
the existing close/root/reap results must have succeeded, and a completion task
waits for the same actual reader to exit. Exact request/domain CAS then wakes
the original scope. Other uncertain cleanup retains its task and diagnostic
evidence; a new cache, elapsed grace or retry count cannot clear it.

The original service consumes a late reader or owner-completion failure. It
reports the unknown immediately and raises the original failure at stop/join;
a delayed result never becomes a synchronous exception in an already returned
invocation. The exact scope remains unresolved after failure. A later turn has
its own scope even when it uses the same cached model.

## Remaining boundary

Only the original host's serialized turn pump observes an applied force. If that
host dies first, no successor boot or sweep settles the force on its behalf: the
agent stays terminated with its lifecycle pointer set, resurrecting it is
refused, and the hourly stuck-lifecycle-pointer scan
(`services/upkeep/ttl_reaper/lifecycle_fences.py`) raises
`lifecycle_pointer_stuck` for an operator. The operator inspects any leftover
`req-*.json` envelope with `base/agents/incarnation/exec_request_evidence.py`
(`disposition_hint` prints the commands), then settles the command and clears
the pointer by hand. An exec request envelope is created before a disposable exec
child and removed after close, root reap, and reader completion, so it is the
durable resource witness.

This does not establish hard-host-death recovery with active exec: independent
POSIX children may survive and the parent's unreaped root pin may be lost, and
host PID/birth, lease expiry, or owner UUID alone cannot prove managed-domain
completion. Request leftovers are not age-pruned; normal exact
resource settlement removes them. The original host's settlement re-locks the
exact target generation, owner, and command before clearing its active pointer.
Persistent shell sessions deliberately retain their separate ownership and are
not disposable exec descendants to kill wholesale.

## Contract verification

The host force tests exercise real thread work, exec children and late readers.
Reader delay and bounded join exercise the actual retained output pipe and
service task handles, while preserving force observation and successor isolation
assertions. An uncooperative actual task proves finite service return and pool
retention through the existing daemon hard exit. History owner tests verify that an incomplete settled-write
scan propagates its failure; host reconciliation preserves the claimed row when
the public committed-id resolver fails.
