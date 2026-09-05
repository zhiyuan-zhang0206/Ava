---
type: doc
title: Cooperative agent impersonation
description: Same-machine DB leases, native consent and checkpoint ownership, external inbox ACKs, and return handoffs.
tags:
- shared
- identity
- lifecycle
---

# Cooperative agent impersonation

`shared/impersonation.py` implements a local trusted-controller protocol.
`agent_impersonations` is the authoritative lease and ordered plugin journal;
`agent_impersonation_messages` records which inbox rows that lease has read and
acknowledged. The raw random capability is returned once. Only its SHA-256 hash
is stored. A capability and the agent's local machine identity are required for
external operations. This is cooperative identity binding for processes already
holding cluster credentials, not a sandbox for untrusted Python or native tools.

## State and ownership

`requested -> accepted -> active -> released | expired`; native rejection goes
from requested to rejected. The request deadline prevents abandoned requests
from blocking later ones. The active TTL starts only after native acceptance,
execution resource closure and checkpoint flush. Explicit renewal replaces the
deadline; neither attaching nor relaying renews it. Database-clock checks after
locking decide expiry. New requests serialize on the agent row and cannot
overlap an existing request, active lease or unapplied plugin journal.

Requests stay in the protocol's private table until the native claim gate
renders their real external sender and asks for consent. Native acceptance ends
the current execute-code call. Only the invocation driver can activate after
the result is durably checkpointed. A replacement incarnation before activation
increments the consent version and asks again; a fully active lease survives
native process/host restarts. Updated native drivers gate recovered graph nodes,
automatic compaction and normal inbox claims. Hosted agents release their turn
slot while paused. Administrative restart/terminate still reach the
native lifecycle dispatcher, without consuming ordinary chats. Every database
termination writer revokes the lease through the same status transaction's
trigger; restart preserves it. Native status metadata remains native: an active
controller's busy/idle turns are not mirrored. Idle watchers can observe the
parked runtime as idling; external completion requires an explicit message or
the return handoff.

`ava.external.attach` binds identity in the external Python process and hydrates
the existing checkpoint, pinned config and plugin state. It validates identity
and plugin-state accesses against the lease. SDK operations execute directly.
The native graph remains the only checkpoint writer: external state updates
append serialized reducer inputs with a version CAS. On return, the driver
applies those deltas and checkpoints a lease/version receipt before acknowledging
the journal. A crash between those steps replays the receipt, not the mutation.
Native shell/editor actions outside AVA are not intercepted or rolled back.

## Messages and wakeups

Peers continue addressing `agent:<id>`. Redis's existing per-agent inbound
channel supplies wake hints; the database supplies messages after connection
loss. External inbox reads leave messages pending. Only an explicit ACK after
processing marks them done. Cancel requests are also external inbox entries:
the native control-only claim leaves them pending for the controller to process.
Unacknowledged messages and cancellations return to the native agent.
Newly acknowledged cancellations publish the existing `Cancelled` event after
commit; repeated ACKs do not emit another completion event.
Host relay delivery is at least once: a relay restart repeats pending hints.

Voluntary release atomically inserts a normal inbound with the actual external
sender and summary, then closes the lease. This narrow handoff follows explicit
native consent to this protocol; it does not enable generic caller-protocol v1
or weaken the existing external-message/lifecycle write fences. Expiration
instead records a system notice, without inventing an external summary. Redis
wakes occur after commit. Migration rollback refuses outstanding leases,
unapplied deltas, and unconsumed handoffs.

The existing gateway TTL reaper expires abandoned leases even when the runner
is offline. Native gates and external SDK calls independently enforce the
deadline. The same reaper removes completed lease/capability/journal records
after seven days, only after state application and handoff consumption; the
handoff inbound remains in normal history.

See [[../ava/external.ava.okf.md]],
[[../conventions/agent-impersonation.md]], and
[[../conventions/agent-impersonation-hosts.md]].
