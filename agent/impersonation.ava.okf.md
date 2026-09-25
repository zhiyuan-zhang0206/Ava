---
type: doc
title: Cooperative external impersonation
description: Automatic safe-boundary takeover, durable native session-end notes, and external plugin state restoration.
tags: [agent-lifecycle, concurrency]
---

# Cooperative external impersonation

`agent/impersonation.py` connects the durable lease in
`shared/agents/impersonation/` to the native graph. The native runtime remains the
only checkpoint writer; an external process executes the SDK directly.

The claim gate accepts named automatic requests without a model decision and
ends the invocation. The driver drains resources, reconciles the session's
checkpoint marker (including an accepted request whose initial write failed),
flushes it and verifies relay readiness before activation. Hosted turns return
their slot and reject ordinary active-session wakes before runtime preparation.
The legacy consent version and accept/reject calls remain available only for
requests already created by older clients during an upgrade.

`supervise_relay` is the native supervision seam, called from two places: the
claim gate (native loop paused or resuming) and the held-controls pass
(services/agent_host/host.py `_apply_held_controls`) while an active lease
parks the agent outside the graph — the dispatcher's pending scan wakes rows
with an open lease periodically, pull-based from the database, so supervision
does not depend on wake delivery. It stops the takeover when a core component
died (task #3998): all recorded controller anchors dead/reused (a single
unreadable pass waits for a second consecutive pass; no anchors is skipped),
or a relay heartbeat stale past 45 seconds. The one exception is a codex relay
minted by an earlier incarnation whose last beat predates this process start,
inside the fresh-start window (`AVA_IMPERSONATION_REPROVISION_WINDOW_SECONDS`,
0 disables) — re-provisioned and respawned instead of stopped; a claude relay
is never re-provisioned. Stopping is the abort path: terminal `expired` with
`aborted: <detail>` in rejection_reason, a held relay process terminated,
reminders dismissed, and the resume chain's end note names the cause. The hot
path is one native-status read, the anchor classification and a heartbeat
comparison.

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

Tests: `tests/agent/test_impersonation.py` covers gates, receipt recovery, the
component-death judgments and the fresh-start window;
`tests/agent/test_impersonation_integration.py` exercises PostgreSQL, buffered
checkpoints, the compiled graph, a real exec child, peer inbox acknowledgement,
release summary, native resumption with plugin state, and the abort→resume
chain including the death-caused end note.

`agent/impersonation_handoff.py` saves one JSON file under the agent workspace,
then appends the impersonator summary and path as the first new system note.
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
resume note directs the native agent to `event_delivery`: pending coverage is
unknown, so zero consumed events is not a zero-call claim. The upstream
manifest's `complete_emitted_events` coverage is only a census of emitted
events; SDK sampling policy remains unknown, so even a completed zero SDK count
is not a zero-call claim.
