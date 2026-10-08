---
type: doc
title: Cancel and Compact Acceptance
description: Transactional operation identities preserve original native inbound acceptance without claiming execution recovery.
tags: []
---

# Cancel and compact acceptance

`POST /api/cancel` and `POST /api/agents/{agent_id}/compact` accept an optional
`Idempotency-Key`. `gateway.http.auth.request_principal.optional_request_key` owns
validation and credential scoping. One key identifies one operation in that
scope. A principal-v1 key includes the verified credential, method and actual
path: the same raw key on different compact agent paths or credentials is a
different scope. On the shared `/api/cancel` path, changing `agent_id` for the
same scoped key conflicts with 409. Legacy keys retain their raw namespace,
partitioned by actual path. Unknown/empty keys or scopes fail before writes;
missing keys preserve one-shot legacy behavior.

`base/agents/messages/control_delivery.py` owns acceptance. Its transaction
serializes the scoped identity, checks an existing receipt before mutable agent
state, then locks `agents_meta` before checking status and inserting a native
inbound. Termination and resurrection use the same status row lock. The inbound,
its audit fact and `agent_control_receipts` snapshot commit together. Cancel on
a terminated agent records `already_terminated` without an inbound. Replaying
that no-op after resurrection keeps its original result and cannot pause new
work. A deliberate new request uses a new key.

Responses retain their existing `status` and add `inbound_id` (null for a cancel
no-op). An acceptance is not an applied or observed command. The receipt stores
only original agent/kind, result, inbound ID and acceptance time; it has no work
queue, worker, foreign key or expiry. Positive identity snapshots survive later
queue or agent deletion. A retry never replaces a missing inbound with new work.
Future receipt retirement needs explicit expired-key semantics before pruning.

Live hints follow commit. Retry may repair a wake only while the exact original
inbound is pending. Compact auto-resurrection uses that original ID and the
existing `COMPACT_REQUEST` pending-work guard, including termination/force
cutoffs. A consumed or deleted inbound replays acceptance without waking,
resurrecting or re-enqueueing. Recovery hints cannot establish kernel completion.

The browser allocates an operation ID per invocation and accepts an explicit
ID for same-intent recovery. CLI calls allocate one ID per invocation; SDK POST
transport follows the route contract. These clients add no outbox or automatic
ambiguous retry against an unproven older gateway. Keyless callers remain
supported; new gateways do not make older gateways recognize the header.

## Remaining native execution boundaries

This owner does not add a work-episode fence. Runtime generation spans multiple
hosted turns; it cannot identify the current episode alone. Existing cancel and
compact claim semantics mark non-chat rows done before halt/compaction/checkpoint
application, so `done` is not proof that those effects occurred. Recovery after
that claim/application crash window and stale commands crossing work episodes
remain separate lifecycle work under issue #4474. Restart/terminate acceptance,
incarnation ownership and resurrection authority are unchanged.
