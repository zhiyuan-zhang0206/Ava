---
type: doc
title: Impersonation ownership and return
description: Lease state transitions, renewal, native return, and operator closure.
tags:
- shared
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
The gateway force-expire endpoint closes only the open session number supplied
by its caller. It records an operator cause and actor, dismisses reminders,
queues a resumed-turn note for a live non-automatic session, and publishes a
native wake. A stale session number returns `not_open` without touching a newer
session. The external executor remains outside this DB action.

Explicit renewal replaces the database deadline; attaching and relay heartbeats
never renew. The existing TTL reaper expires abandoned sessions even when the
runner is offline, and sends a reminder once per approaching deadline. This is
coordination among processes already holding local cluster authority, not a
security boundary against arbitrary shell execution. Capability, machine,
incarnation and caller-attestation checks still prevent accidental
cross-session control.

`ava.external.attach(session_id, agent_id=...)` loads saved state and
binds SDK identity in the external process. Plugin changes append ordered deltas;
only the native graph writes checkpoints. On return it applies the journal with
a durable lease/version receipt. It then writes the handoff file, checkpoints
the first resumed system note, and finally marks the handoff applied in the DB.
A crash retries the same note identity and flushes even when its checkpoint
receipt is already visible. New sessions and ordinary input remain gated until
this receipt succeeds. File or checkpoint failures cannot resume native work.
