---
type: doc
title: Cooperative external impersonation
description: Automatic safe-boundary takeover, durable native handoff notes, and external plugin state restoration.
tags: [agent-lifecycle, concurrency]
---

# Cooperative external impersonation

`agent/impersonation.py` connects the durable lease in
`shared/impersonation.py` to the native graph. The native runtime remains the
only checkpoint writer; an external process executes the SDK directly.

The claim gate accepts named automatic requests without a model decision and
ends the invocation. The driver drains resources, reconciles the session's
checkpoint marker (including an accepted request whose initial write failed),
flushes it and verifies relay readiness before activation. Hosted turns return
their slot and reject ordinary active-session wakes before runtime preparation.
The legacy consent version and accept/reject calls remain available only for
requests already created by older clients during an upgrade.

`supervise_relay` is the native supervision seam for the bound relay, called
from two places: the claim gate (native loop paused or resuming) and the
held-controls pass (services/agent_host/host.py `_apply_held_controls`) while
an active lease parks the agent outside the graph — the dispatcher's pending
scan keeps waking agents with an open lease, so the held path re-checks the
relay heartbeat periodically even without inbound traffic. The hot path is one
native-status read plus a heartbeat comparison; only a stale heartbeat
escalates to provision, spawn, or the rate-limited failure stamp. A dead codex
relay is respawned (or re-provisioned after a host turnover); a claude relay's
failure is stamped for the controller's own supervision.

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
begin durable handoff before reopening the ordinary input path.

Cancel requests remain pending in the external inbox while held. The controller
stops its current work and explicitly acknowledges the request; an unacknowledged
cancel remains in the handoff JSON for the resumed native agent when the lease ends. The native
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

`agent/impersonation_handoff.py` saves one JSON file under the agent workspace,
then appends the impersonator summary and path as the first new system note.
The stable note id and `impersonation_handoff_id` channel survive retries.
Checkpoint flush is unconditional before setting `handoff_applied_at`; only
that receipt consumes captured pending input and opens the normal claim gate.
The file retains incoming ACK state and every recorded body. Input arriving
after release remains queued behind the note. File or checkpoint errors retain
the native gate. An unavailable event backend leaves accounting explicitly
pending without blocking the control handoff; the registered agent-host event
reconciler supplements the same file after late events become readable.
