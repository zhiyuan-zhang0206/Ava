---
type: doc
title: Named agent impersonation sessions
description: Trusted same-machine controllers, per-agent session numbers, permanent messages, and durable native handoffs.
tags:
- shared
- identity
- lifecycle
---

# Named agent impersonation sessions

`shared/impersonation_sessions.py` exposes `(agent_id, session_id)` handles.
Each agent allocates increasing integers starting at zero, through a database
counter and allocation trigger, including when an older client inserts a row.
The session `name` and free `executor_name` are separate from the CLI's observed
process metadata (PID, name, executable, birth time and ancestors). `relay_provider`
selects transport; no name or process observation proves a provider's identity.
The former UUID remains a private compatibility reference for existing leases,
checkpoint receipts and plugin journals. Public commands and file paths
use the scoped integer. Credentials are returned once and stored as hashes.

## Ownership and return

The public state machine is `preparing -> active -> released | expired | rejected`.
Internally, preparing uses requested/accepted to preserve the drain handshake.
A trusted controller does not require a native model approval. The native claim
gate accepts automatically, ends that invocation, drains execution resources,
repairs and flushes a checkpoint timeline anchor, verifies the relay heartbeat,
then activates. Only one executor owns decisions. A new native incarnation
reconciles preparation; active control survives native restarts. Administrative
restart/terminate still reach the native dispatcher; termination revokes control.
Legacy live requests retain their original consent flow during an upgrade.

Explicit renewal replaces the database deadline; attaching and relay heartbeats
never renew. The existing TTL reaper expires abandoned sessions even when the
runner is offline, and sends a reminder once per approaching deadline. This is
coordination among processes already holding local cluster authority, not a
security boundary against arbitrary shell execution. Capability, machine and
incarnation checks still prevent accidental cross-session control.

`ava.external.attach(session_id, agent_id=..., token=...)` loads saved state and
binds SDK identity in the external process. Plugin changes append ordered deltas;
only the native graph writes checkpoints. On return it applies the journal with
a durable lease/version receipt. It then writes the handoff file, checkpoints
the first resumed system note, and finally marks the handoff applied in the DB.
A crash retries the same note identity and flushes even when its checkpoint
receipt is already visible. New sessions and ordinary input remain gated until
this receipt succeeds. File or checkpoint failures cannot resume native work.

## Permanent messages and unified timeline

`agent_impersonations` retains every session and lifecycle endpoint.
`agent_impersonation_entries` retains immutable, sequenced lifecycle, inbound,
outbound, SDK and API records. DELETE guards protect sessions; UPDATE/DELETE
guards protect entries. There is no retention cleanup. Migration rollback
refuses to discard recorded history. The export is regenerable from the DB.

Activation captures pending input, and committed inbound inserts capture arrivals
while active. Idempotent chat retries produce one history entry. Inbox reads leave
messages pending; explicit ACK records processing without removing their bodies.
`ava impersonate say` commits an outbound message with
a stable retry key, then publishes `impersonation_changed`. Logical identity
remains the Ava agent; `impersonation` metadata names the session and executor.
Incoming user messages remain incoming messages.

The existing timeline endpoint hydrates the checkpoint's session anchor with
bounded pages of retained message entries. Numeric block cursors preserve ordering
inside a session and historical compact segments. The frontend uses its existing
timeline query and merge machinery; the refresh event does not carry another
message store. The card header does not display an executor or session badge;
the impersonation metadata stays on the timeline item.

## Structured handoff

One session produces `<workspace>/impersonation/<session_id>.json` containing
session/process metadata, all input/output bodies and inbound ACK state,
lifecycle facts, original consumed SDK/API events and statistics. Normal release
requires the impersonator's own summary. Expiry/rejection states their reason and
absence of an external summary. The first new system note contains that summary
and the JSON path, before ordinary queued input. Captured pending inputs become
processed only after the note is durable; unacknowledged incoming content remains
in the file for the resumed agent to handle.

SDK collection, sampling and instrumentation belong to the upstream collector.
The consumption boundary is `shared/impersonation_events.py`: explicit scoped
session binding, stable event IDs, time validation, deduplication and export
refresh when late facts arrive. The agent-host background reconciler selects
one due local session and consumes at most four pages per pass; a saved cursor
continues large sweeps, and completed sweeps restart to recover late indexing.
`complete_delivery` accepts an upstream manifest of exact event IDs, validates
the durable set and certifies completion. Until then, accounting remains pending
even after the native handoff receipt. Statistics count recorded events without
extrapolating samples. See the consumer module for delivery-completion semantics.

See [[../ava/external.ava.okf.md]],
[external agent procedure](../conventions/agent-impersonation.md), and
[host relay setup](../conventions/agent-impersonation-hosts.md).
