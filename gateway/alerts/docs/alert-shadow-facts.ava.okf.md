---
type: doc
title: Alert Notification Shadow Facts
description: "Inactive, immutable alert transition and group observations committed with webhook ingest; no native delivery activation."
tags:
- gateway
- alerts
---

# Alert Notification Shadow Facts

The first ingest transaction also records immutable shadow transition/group facts
through `base.telemetry.alerts.shadow.AlertShadowBatch`. Fingerprints are
transaction-locked in sorted order before resolving instance start times and
locking actual rows. This slightly broader gate also serializes absent instances
and concurrent latest-start lookup. Resolution, decisions and rendering retain
POST input order: an earlier insertion is visible to a later missing-start entry.
The resolved natural key is reused by the upsert. Unknown instances without a
start time remain skipped. This owner acquires fingerprint gates before row
locks; there is no reverse lock path or lock across provider calls.

`alerts.notification_revision` identifies notification transitions, while
`alert_notification_groups` freezes the existing group renderer, display language,
text and member order. Each `(alert_id, notification_revision)` belongs to one
group. Repeated still-firing observations whose legacy delivery is unconfirmed
reuse that revision even if severity or annotations change. The live alert row
still updates, and the legacy sender still renders the new observation. Those
changes do not rewrite an immutable group or create a text-derived operation
identity. This inactive slice does not promise a distinct shadow transition for
every severity drift while `notified_at` remains NULL; native activation must
explicitly decide that policy. A repeated A+B group stays frozen when a later POST adds C;
only newly eligible C becomes a new group. An omitted member does not cancel old
facts. Repeated same-instance entries are processed in original input order,
with equal observations producing one fact; distinct qualifying transitions keep
separate revisions. Existing unconfirmed instances first observed after the
schema upgrade are marked `legacy_unconfirmed`, not backfilled as fresh history.

These records are **shadow observations**, not accepted outbound work or evidence
of provider delivery. The current legacy grouped `/send`, SSE, HTTP response and
`notified_at` behavior remains unchanged. Legacy sends can already have delivered
any shadow fact. A future native delivery cutover must stop legacy producers and
old IM generations, drain active provider calls and explicitly delimit eligible
new operations; it must never automatically enqueue shadow history. No expiry,
recipient selection, provider attempt, retry worker or new RPC is enabled here.
A future versioned acceptance endpoint must distinguish its durable receipt from
legacy `/send` HTTP 200. Ops delivery idempotency remains open in #4477.
