---
type: doc
title: Guarded launch retry acceptance
description: A versioned retry intent fixes one launch attempt and recovers only its existing queue wake.
tags: [gateway, agents, idempotency]
---

# Guarded launch retry acceptance

`POST /api/keyed/v1/agents/{agent_id}/retry-launch` requires a valid
`Idempotency-Key`, exact `Idempotency-Scope: principal-v1`, a verified credential
principal and an explicit UUID `expected_prior_attempt_id`. Selected-agent
`AgentSnapshot.last_launch_attempt_id` exposes the observed prior attempt;
missing data from an older gateway supplies no evidence for strong admission.
Every request authenticates again. An old gateway has no versioned route; an
old runner rejects the new `launch-reconcile-v1` kind before effects. Neither
failure authorizes fallback to the original HTTP path or `spawn-launch-v2`.

`ops.agents.launch_retry.accept_retry_launch` owns the transaction. It serializes
the scoped key, looks up the receipt before mutable target state, then locks
`agents_meta`. A fresh request needs the matching current attempt, idling status
and no admission observation. Stale or admitted targets return 409; missing fresh
targets return 404. The original prior/new UUIDs, placement, config overlay,
birth config and acceptance snapshot commit with the pointer rotation. Same-key
changed intent conflicts. Replaying the original intent keeps its original
acceptance, including after target deletion, admission or a later deliberate
retry. A deliberate new attempt needs a new key and the latest observed UUID.

`agent_launch_retry_receipts` has no target foreign key or expiry. Its fixed
attempt identities are never pruned or replaced by a cache. A receipt is
historical acceptance, not proof of URL/target existence, runner support, model
validation, prompt claim, host admission or a completed turn. Config snapshots
record acceptance facts; replay neither resolves changed defaults nor reverts
later operator configuration. Native host configuration/admission remains owned
by the existing host path.

`ops.lifecycle.launch_reconcile` reads the committed receipt and locks the
metadata row through `base.db.publish_inbound_wake`: only matching local machine,
current attempt, idling status and null admission may publish. Missing,
terminated, admitted, moved or superseded targets produce no wake. The publisher
uses existing Redis pub/sub plus the wake breadcrumb; its impersonation alert
performs a plain read, without acquiring a host-owner or metadata lock. The
reconciler does not acquire a host-owner lock, so it introduces no inverse lock
order. Concurrent admission/termination/rotation waits on the same metadata row.
An already published hint may be consumed later; the existing host rechecks
placement/status, external lease and incarnation admission, and the scheduler
coalesces hints for existing durable inbound work. No prompt insertion, force
restart, resurrection or attempt rotation occurs in reconciliation.

The new kind is naturally repeatable and does not acquire an Ops transport
claim. Native crash before wake and lost RPC result acknowledgement can repeat
the hint with the same fixed attempt. Existing v2/kind canonical keys and all
uncertain Ops claims remain unchanged; this does not reinterpret an uncertain
v2 result as execution evidence. HTTP acceptance survives an unsupported or
unreachable runner, which remains a delivery gap until a later same-intent
reconciliation or the existing pending scan. No new durable worker is added.

The original `/api/agents/{agent_id}/retry-launch` route and its one-shot
attempt-rotation owner are removed. The browser API and SDK require an explicit
observed UUID and caller key for the versioned route. Launch-failure responses
advertise that route. See [[ava/agents/docs/launch-retry.ava.okf.md]]. There is no
capability cache, client outbox or automatic ambiguous-failure retry; recovery
must reuse the original intent. Installed older consumers are not supported by a
fallback route.
