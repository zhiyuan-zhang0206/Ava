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

`shared/agents/impersonation/impersonation_sessions.py` exposes `(agent_id, session_id)` handles.
Each agent allocates increasing integers starting at zero, through a database
counter and allocation trigger, including when an older client inserts a row.
The session `name` and free `executor_name` are separate from the CLI's observed
process metadata (PID, name, executable, birth time and ancestors). `relay_provider`
selects transport; no name or process observation proves a provider's identity.
The former UUID remains a private compatibility reference for existing leases,
checkpoint receipts and plugin journals. Public commands and file paths
use the scoped integer. The controller holds no credential: its authority is
the session id plus caller presence, attested against the recorded process
tree. Relay credentials are returned once and stored as hashes.

## Ownership and return

See [[shared/agents/impersonation/ownership.ava.okf.md]] for the lease state machine,
native return, renewal, and operator closure.

## Relay binding

`shared/agents/impersonation/relay.py` owns relay credential provisioning and
re-provisioning, relay reads, heartbeats, failure stamps, and aborting a lease
when a component dies. The package door exposes this surface to callers through
`shared.agents.impersonation`.

## Bounded delivery

`shared/agents/impersonation/impersonation_delivery.py` reserves host submissions against the
lease's `max_delivery_attempts` and `ack_window_seconds`. Requests snapshot
`impersonation_max_delivery_attempts` and `impersonation_ack_window_seconds`
from cluster config (defaults: 2 total attempts, 180 seconds per ACK window).
Later config edits apply to new leases. Migration preserves the 300-second
window of existing leases. Attempt count, policy and database reservation time
survive restart and relay credential rotation. The relay
credential can reserve delivery but cannot ACK, renew, or arbitrarily release.
Lease reads and native reconciliation expire an exhausted takeover with the
missing-ACK cause through the existing handoff. Expiry checks all pending rows,
not just the relay page. New arrivals and unrelated ACKs do not reset a budget.
A submission failure spends its reserved attempt and retains the pending body.

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

## Session record

One session produces `<workspace>/impersonation/<session_id>.json` containing
session/process metadata, all input/output bodies and inbound ACK state,
lifecycle facts, original consumed SDK/API events and statistics. Normal release
requires the impersonator's own summary. Expiry/rejection states their reason and
absence of an external summary. The first new system note contains that summary
and the JSON path, before ordinary queued input; it is the resumed input — while
it is the newest message, the agent's claim runs its first turn even with an
otherwise empty queue, and delivery ends by publishing a wake. Captured pending
inputs become processed only after the note is durable; unacknowledged incoming
content remains in the file for the resumed agent to handle.

SDK collection, sampling and instrumentation belong to the upstream collector.
The consumption boundary is `shared/agents/impersonation/impersonation_events.py`: explicit scoped
session binding, stable event IDs, skew-guarded time validation, deduplication
and export refresh when late facts arrive. Protocol-v1 controller receipts
capture event identities before telemetry enqueue, central transactional audit
producers stage their expected identities before commit, and the owning
agent-host alone invokes the database certification procedure after the frozen
union exactly matches tagged central rows and durable entries. Until then,
accounting remains pending even after the native receipt. Manual and legacy
leases never infer an empty receipt. Version-2 handoff statistics expose
`event_delivery.state`, its manifest-only `completion_basis`, and separate
SDK/API `coverage` plus `consumed_event_count`, with an additive
`pending_reason`. Pending coverage is `unknown`:
a zero consumed count is not evidence of zero calls. `complete_emitted_events`
means the manifest covers emitted events only; the SDK sampling policy is
explicitly `unknown`, so a zero SDK count is never a zero-call fact. Statistics
never extrapolate samples.
See the consumer module for delivery-completion semantics.

Manifest authority, alert surfaces, and the post-completion integrity window
are specified in [[manifest-certification.ava.okf.md]].

## CLI parameters: explicit, with four named exceptions

The `ava impersonate` tree spells out every parameter that decides behavior
(user ruling 2026-09-20; task #4102): `request` requires `--ttl` and
`--batch-window`, `renew` requires `--ttl`, and a missing value is a usage
error before any command runs. `send` carries the lease form — it delivers as
the borrowed `agent:<id>`, attested by the controller session. Four options
keep a default because one value is the only reading:

- `list` / `inbox` `--limit` 100 — presentation only; the service validates
  1..1000 (task #3696 exception inventory).
- `inbox` `--wait` 0 — the unique "return immediately" value; any other number
  waits, so the default cannot be confused with consent to block.
- `say` `--phase` `commentary` — the in-progress reply is the routine phase;
  `final` is a deliberate close.
- `relay` `--debounce` 0.5 — internal host machinery (the relay coalesces wake
  hints); not an operator parameter.

See [[ava/external.ava.okf.md]],
[external agent procedure](../../../conventions/agent-impersonation.md), and
[host relay setup](../../../conventions/agent-impersonation-hosts.md).
