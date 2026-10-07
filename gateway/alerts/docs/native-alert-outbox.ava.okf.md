---
type: doc
title: Native Alert Outbox
description: "Qualified native alert groups share atomic frozen fanout acceptance and the existing IM Outbox lifecycle."
tags:
- gateway
- alerts
---

# Native Alert Outbox

The webhook's existing fingerprint gates, input-order resolution, renderer and
revision/member identities remain the source owner. New eligible groups created
while `alerts.im_notify_enabled` is true carry immutable `origin=native-v1`.
Existing `origin=shadow` facts are never promoted or automatically sent. A legacy
unconfirmed member stays shadow even in a POST containing fresh members: origin
partitions preserve each subset's original order and render only its own members.
Repeated native A+B preserves the original group/body; adding C creates only C's
new group. Annotation or unconfirmed severity drift does not create a text-derived
operation. Missing start times retain the established source-resolution rules.

This is creation provenance, not acceptance or provider delivery. There is no
activation epoch or additional enable flag. Disabled observations may produce
shadow facts; enabling later does not backfill them. Native facts already created
remain eligible across a pause/resume. Deliberate re-notification of shadow history
requires a separately designed operation; viewing history never enqueues it.

## Acceptance and recovery

`notify_alert_group` calls only authenticated `POST /send/alert-outbound-v1` with
strict positive `group_id` and `source_origin=native-v1`. The daemon reads the
immutable source body from Postgres; caller text, unknown fields and origins are
rejected. A 404 from an old daemon is a hold, never fallback to legacy `/send`.
Errors log safe classification, not URLs, credentials or context tokens.

`AlertOutboundBridge` resolves all loaded adapters' current owner/account/rendering
without sending. Telegram uses configured owner plus authenticated bot ID,
Weixin the QR-login user plus existing account namespace, Feishu the last known
p2p open ID plus configured app ID. At least one owner must qualify. A receipt
freezes the available recipient subset and explicit unavailable channel/account
(where known)/reason decisions. Unavailable channels carry no fabricated intent.
One transaction commits that receipt and every available shared outbound intent.
All-unavailable leaves no acceptance and may qualify on a later preparation.

The group ID is the operation identity. Replay reads retained receipt/request
before mutable source or owner state, including after source retention. It returns
the original intent IDs and fanout even if accounts, recipients or config change.
No later owner discovery adds a channel, and old-account intents remain held by
the existing dispatcher. Receipts have no queue/source foreign key or expiry.
Native intents have real `agent_id=NULL`; other source kinds require a positive
agent. Postgres 17 `UNIQUE NULLS NOT DISTINCT` protects concurrent NULL context
insertion. No alert ID or zero is disguised as an agent ID.

The existing daemon outbound round also recovers committed native groups lacking
receipts by anti-join. Thus a crash after ingest commit does not require another
webhook or reliable SSE/RPC. A bounded rotating keyset scan advances over held
preparations and wraps; its memory hint is never an acceptance watermark, so low
IDs committed late remain eligible. Shadow origin is excluded before scanning.
`im_notify_enabled=false` holds new acceptance/recovery; already accepted work
retains its durable promise and drains through the existing worker. Operators use
maintenance admission to stop new provider attempts and drain active calls.

## Delivery and completion

The same `IMOutboxWorker`, table and `4477/im-timeline-outbox` transaction gate
own timeline, notice and alert sends. The gate holds across the external call;
short independent transactions persist SENDING before provider access and commit
attempt-CAS outcomes. Unresolved SENDING becomes UNCERTAIN, without TTL stealing
or automatic replay. Any possible acknowledged prefix/ack loss is uncertain;
only adapter proof of no effect may fail. Later recipient messages can continue.
No separate queue, worker, per-chunk retry or resend UI is introduced.

Any real channel SENT commits `alerts.notified_revision` only if the source member
still matches the current alert revision and status. An old attempt cannot stamp
a newer refire/resolution. `notified_at` retains the established first-ever send
time (the old writer filled only NULL and transitions never cleared it). Legacy
timestamps do not fabricate native completion revisions. Acceptance/QUEUED/
SENDING/UNCERTAIN do not stamp either fact. One channel SENT with another uncertain
retains the historical any-channel-success semantics and an explicitly partial
fanout receipt; it is not evidence of delivery to every channel. The ingest
`notified` count uses actual matching completed revisions, never accepted count.

## Rollout boundary and remaining scope

Before activating this code, the operator must stop/drain all old webhook
producers and old IM daemons, including in-flight legacy postcommit `/send` calls,
and verify the running provider/daemon generation and capabilities. Old producers
do not write new receipts; database provenance cannot fence their external sends.
Mixed old/new alert generations are unsafe. Merge does not authorize deployment.
Do not automatically fall back to legacy on rollback or native failure.

Legacy `/send` remains for other immediate notifications. Only native alert webhook
groups are covered here, not every health/escalation producer, manual notification,
explicit notice listing, command or arbitrary Ops event. Provider dedup guarantees
remain those of each adapter: this lifecycle does not promise exactly-once delivery
or retry ambiguous sends. No deployment or automatic replay of history is included.
