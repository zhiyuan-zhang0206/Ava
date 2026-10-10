---
type: doc
title: Cooperative external impersonation
description: Automatic safe-boundary takeover, durable native session-end notes, and external plugin state restoration.
tags: [agent-lifecycle, concurrency]
---

# Cooperative external impersonation

`agent/impersonation.py` connects the durable lease in
`base/agents/impersonation/` to the native graph. The native runtime remains the
only checkpoint writer; an external process executes the SDK directly.

The first takeover in a native conversation appends an explanation of borrowed
identity, exclusive execution, message/SDK routing and the return handoff before
the session's start marker. `impersonation_introduced` is committed with those
messages, so retries and later leases do not repeat the introduction. It defaults
to false for older checkpoints. The context-establishment node restores the
explanation after compaction without claiming a lease is active; agents that
have never encountered a takeover carry no impersonation guidance.

The claim gate accepts named automatic requests without a model decision and
ends the invocation. The driver drains resources, reconciles the session's
checkpoint marker (including an accepted request whose initial write failed),
flushes it and verifies relay readiness before activation. Hosted turns return
their slot and reject ordinary active-session wakes before runtime preparation.
The legacy consent version and accept/reject calls remain available only for
requests already created by older clients during an upgrade.

Relay recovery uses existing claim and held-control supervision opportunities;
see [[agent/docs/impersonation/relay-supervision.ava.okf.md]] for exit evidence,
transport replacement and delivery boundaries.

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
Termination also queues ordered native system notes: impersonation interrupted,
then completed termination. Ordinary claim renders these before the resurrection
marker. Resurrection and its prompt use the database clock after locking the
agent; claim orders by creation time and id, including equal-time notes.
The closing lease reason drives the independent host scan's durable notice
without reopening expired inbox authority or delaying native handoff.

Cancel requests remain pending in the external inbox while held. The controller
acknowledges the request on receipt, then stops its current work; the cancel
remains in the handoff JSON for the resumed native agent when the lease ends. The native
dispatcher cannot interrupt an external host's in-flight tools.

External plugin deltas are an ordered lease log using the checkpoint codec.
On return, the native driver applies each delta through `graph.aupdate_state`
with its `{lease_id, version}` receipt in the same checkpoint, flushes, and
marks the log version applied. Recovery skips checkpoint-receipted versions,
so a crash between checkpoint and acknowledgement cannot apply an additive
reducer twice. Core lifecycle fields cannot be changed by plugin deltas.

Tests: `agent/tests/impersonation/test_impersonation.py` covers gates, receipt recovery, the
executor-death judgments and fenced delivery recovery;
`agent/tests/impersonation/test_impersonation_integration.py` exercises PostgreSQL, buffered
checkpoints, the compiled graph, a real exec child, peer inbox acknowledgement,
release summary, native resumption with plugin state, and the abort→resume
chain including the death-caused end note.
`agent/tests/impersonation/test_impersonation_transport_integration.py` combines real
PostgreSQL and a compiled graph with controlled process-query failures and
stubbed relay establishment. Repeated unknown/permission-denied executor
probes preserve the active lease, original expiry, pending input and delivery
budget without native model calls. Delivery reservations exhaust the push
budget without preventing a valid late ACK. Successor admission fences the
previous runtime, aligns the active lease's native binding, and preserves
unacknowledged input in the handoff record before native resumption. These
tests do not start or restart a real agent host or Codex relay process.

`agent/impersonation_handoff.py` saves one JSON file under the agent workspace,
then appends the impersonator summary and path as the first new system note.
The introduction, takeover, and closing notes include their creation time in
the agent-visible text when message timestamps are enabled, using the same
instant as their timeline metadata. Existing receipts retain their original
notes and timestamps across retries. The process owner supplies `HandoffNotes`
from `base.agents.impersonation.notes`, the shared input contract for controllers
and native graph execution, with a clock factory and a live timestamp-policy
reader. Message construction stays in the agent layer. Constructing the inputs reads
neither input; each new note obtains its clock, captures the creation instant,
then reads the display flag. Native graph notes use their explicit agent context;
invocation settlement carries the same narrow inputs from the host owner.
The stable note id and `impersonation_handoff_id` channel survive retries.
Checkpoint flush is unconditional before setting `handoff_applied_at`; only
that receipt consumes captured pending input and opens the normal claim gate.
The file retains incoming ACK state and every recorded body. Input arriving
after release remains queued behind the note. The note is the resumed input:
while it is the newest message, claim runs its first turn even with an
otherwise empty queue and no conversation yet, and delivery ends by publishing
a wake so a turn ending first still resumes the agent. File or checkpoint
errors retain the native gate. An unavailable event backend leaves accounting explicitly
pending without blocking the control handoff; the registered agent-host event
reconciler supplements the same file after late events become readable. The
resume note identifies ordinary assistant text in the current conversation as
the reply channel, then asks the native agent to review incoming requests and continue work
whose completion is not established, regardless of message receipt status. It
warns that missing activity entries do not prove an action never happened. In the
record, `event_delivery` describes coverage: pending coverage is unknown, so zero
consumed events is not a zero-call claim. The upstream
manifest's `complete_emitted_events` coverage is only a census of emitted
events; SDK sampling policy remains unknown, so even a completed zero SDK count
is not a zero-call claim.
