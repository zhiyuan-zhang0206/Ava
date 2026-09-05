---
type: doc
title: Cooperative external impersonation
description: Native consent, durable pause boundaries, and external plugin state restoration.
tags: [agent-lifecycle, concurrency]
---

# Cooperative external impersonation

`agent/impersonation.py` connects the durable lease in
`shared/impersonation.py` to the native graph. The native runtime remains the
only checkpoint writer; an external process executes the SDK directly.

The claim gate reads the request table and presents the external caller's real
source in a deterministic consent message. The checkpoint stores request ID and
consent version, so compaction cannot duplicate a request and a replacement
incarnation can require new consent. This private negotiation does not activate
the generic caller protocol or bypass its old-writer rollout barrier.

`ava.impersonation.accept` verifies the exec child's captured native incarnation
and ends execution through `AgentImpersonation`. Parent execution resources
close before the exec result enters the graph. Accepted leases end at claim;
the invocation driver activates only after graph return and the optional
N-step checkpoint flush. A surviving process parks outside graph execution,
retaining its liveness renewer and claim progress. Hosted turns return their
slot and reject ordinary active-lease wakes before runtime preparation.

The claim gate leaves chat, heartbeat and compaction input pending while held.
Node guards suppress initialization hooks, automatic compaction, and execution
hooks; cold boot and database recovery defer checkpoint repair while held.
Administrative restart/terminate input uses a control-only claim and
the existing lifecycle apply helpers entirely outside graph execution. Only
an accepted command can leave that claim path; it bypasses ordinary batch
acknowledgement. Invalid directed pending intents fail without settlement;
an accepted intent whose target was replaced receives the existing explicit
`superseded` result. Restart preserves the external lease; termination revokes it atomically
through the database lifecycle trigger. New runtime incarnations read the same
lease before normal execution. Database-clock expiry and explicit release
restore the ordinary inbound path, including unacknowledged external input and
the durable handoff message.

Cancel requests remain pending in the external inbox while held. The controller
stops its current work and explicitly acknowledges the request; an unacknowledged
cancel returns to normal native claim processing when the lease ends. The native
dispatcher cannot interrupt an external host's in-flight tools.

External plugin deltas are an ordered lease log using the checkpoint codec.
On return, the native driver applies each delta through `graph.aupdate_state`
with its `{lease_id, version}` receipt in the same checkpoint, flushes, and
marks the log version applied. Recovery skips checkpoint-receipted versions,
so a crash between checkpoint and acknowledgement cannot apply an additive
reducer twice. Core lifecycle fields cannot be changed by plugin deltas.

Tests: `tests/agent/test_impersonation.py` covers gates and receipt recovery;
`tests/agent/test_impersonation_integration.py` exercises PostgreSQL, buffered
checkpoints, the compiled graph, a real exec child, peer inbox acknowledgement,
release summary, and native resumption with plugin state.
