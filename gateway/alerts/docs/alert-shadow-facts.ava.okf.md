---
type: doc
title: Alert Notification Shadow Facts
description: "Immutable alert transition/group observations and the retained shadow-history boundary."
tags:
- gateway
- alerts
---

# Alert Notification Shadow Facts

The first ingest transaction records immutable transition/group facts
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
still updates. Those
changes do not rewrite an immutable group or create a text-derived operation
identity. The existing policy does not mint a distinct transition for every
severity drift while delivery is unconfirmed. A repeated A+B group stays frozen when a later POST adds C;
only newly eligible C becomes a new group. An omitted member does not cancel old
facts. Repeated same-instance entries are processed in original input order,
with equal observations producing one fact; distinct qualifying transitions keep
separate revisions. Existing unconfirmed instances first observed after the
schema upgrade are marked `legacy_unconfirmed`, not backfilled as fresh history.

Groups whose creation origin is `shadow` remain **unknown historical observations**,
not accepted outbound work or provider-delivery evidence. Legacy may already have
sent them, so native producers never promote or automatically enqueue this history.
New eligible groups may carry `native-v1` creation origin; the sender's retained
receipt and actual SENT completion remain separate facts. See [[native-alert-outbox]]
for atomic fanout, recovery, completion and the mandatory old-generation stop/drain
boundary. There is no expiry or automatic historical replay.
