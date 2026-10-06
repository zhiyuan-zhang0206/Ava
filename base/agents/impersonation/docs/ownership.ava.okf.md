---
type: doc
title: Impersonation ownership and return
description: Lease state transitions, renewal, native return, and operator closure.
tags:
- base
- identity
- lifecycle
---

# Impersonation ownership and return

The public state machine is `preparing -> active -> released | expired | rejected`.
Internally, preparing uses requested/accepted to preserve the drain handshake.
A trusted controller does not require a native model approval. The native claim
gate accepts automatically, ends that invocation, drains execution resources,
repairs and flushes a checkpoint timeline anchor, verifies the relay heartbeat,
then activates. Only one executor owns decisions. A new native incarnation
reconciles preparation; active control survives native restarts. Administrative
restart/terminate still reach the native dispatcher; termination revokes control.
Legacy live requests retain their original consent flow during an upgrade.

Lease status is owned by `base/agents/impersonation/status.py` (`ImpersonationStatus`):
requested, accepted, active, released, rejected, expired. Raw lifecycle query rows
validate their status before dispatch; other row fields keep their existing database
contracts. The open roster projection admits only requested, accepted and active.

Every status transition to `terminated` atomically expires open sessions and
closes event admission. The trigger queues a native impersonation interruption
note followed by a completed-termination note, including when native execution
is drained. Both precede the next resurrection marker; the graph's acceptance
acknowledgement describes the earlier intent separately. Termination dismisses
renewal reminders and records `terminated: agent was terminated` on the session.
The machine host's existing scan independently sends a lease-scoped notice to
its immutable recorded Codex endpoint/thread, even when the relay is dead or a
new lease has replaced it. No inbox authority is reopened. Native restoration
never waits for notice delivery. Host acceptance is persisted; ambiguous timeout
retries may duplicate a notice. Unsupported providers remain explicitly marked,
never described as delivered. Only first open-to-terminal transitions mark a
notice; existing ended history is not backfilled. Restart preserves the lease.
The native pair carries `impersonation_termination_notice: true`; the watchdog
preserves these notes through long terminated periods for later resurrection.

The gateway force-expire endpoint closes only the open session number supplied
by its caller. It records an operator cause and actor, dismisses reminders,
queues a resumed-turn note for a live non-automatic session, and publishes a
native wake. A stale session number returns `not_open` without touching a newer
session. The external executor remains outside this DB action.

Explicit renewal replaces the database deadline; attaching and relay heartbeats
never renew. The existing TTL reaper expires abandoned sessions even when the
runner is offline, and sends one reminder per approaching deadline. Its lead
time is `min(ttl_seconds, max(ttl_seconds * 0.1, 300))` seconds, based on the
current requested or renewed TTL. Delivery follows the reaper scan (default
60 seconds); short leases enter the reminder window immediately. ACK alone
does not cause another reminder; a new renewal deadline can. This is
coordination among processes already holding local cluster authority, not a
security boundary against arbitrary shell execution. Capability, machine,
incarnation and caller-attestation checks still prevent accidental
cross-session control.

Caller metadata records each process and ancestor's PID, native birth, start
ticks and boot identity. POSIX evidence requires an explicit native boot ID;
Linux identity compares positive kernel start ticks within that boot, so a
wall-clock correction does not orphan a live controller. Other platforms use
exact native birth timestamps. Missing or invalid evidence stays unknown and
cannot attest a caller; it is never filled from the current process or converted
to a wall-time fallback. Caller errors distinguish unavailable evidence from
confirmed exit, reuse, and a live controller outside the caller's ancestor chain.

`ava.external.attach(session_id, agent_id=...)` loads saved state and
binds SDK identity in the external process. Plugin changes append ordered deltas;
only the native graph writes checkpoints. On return it applies the journal with
a durable lease/version receipt. It then writes the handoff file, checkpoints
the first resumed system note, and finally marks the handoff applied in the DB.
A crash retries the same note identity and flushes even when its checkpoint
receipt is already visible. New sessions and ordinary input remain gated until
this receipt succeeds. File or checkpoint failures cannot resume native work.

## Heartbeat check-ins

Active, unexpired leases receive ordinary heartbeat messages through the relay
and controller inbox, with the same receipt ACK and retry policy as other
inbound messages. The first check-in is due after the configured idle threshold
from activation (plus jitter); subsequent check-ins follow the heartbeat
interval. External activity does not reset this clock. The borrowed identity
can call `ava.self.pause_heartbeat()` for a known wait or work period; its pause
window also remains effective when native control resumes. Native turn-failure
and no-op backoff do not judge external progress. Preparing leases and terminal
leases with unapplied handoff state stay excluded. Heartbeats are recorded in
permanent impersonation history, and unread messages remain for native return.
Lease-expiry reminders and relay liveness heartbeats have independent clocks:
pausing check-ins neither pauses those signals nor renews the lease.
