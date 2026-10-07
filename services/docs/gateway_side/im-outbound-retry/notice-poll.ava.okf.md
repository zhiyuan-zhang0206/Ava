---
type: doc
title: "Normal Notice Outbound Acceptance"
description: "Notice-specific source receipts reuse the IM intent table and whole-send worker."
tags:
- im-bridge
- idempotency
---

# Normal notice outbound acceptance

`NoticeBridge.poll_once` reads open fleet notices from the existing database,
applying its existing priority/agent filter and FYI age policy. It accepts normal
poll decisions through `NoticePollStore`; it does not call the provider.
`IMOutboxStore` owns the shared intent insertion primitive and `IMOutboxWorker`
owns the same queued/sending/sent/uncertain/failed lifecycle as timeline output.
There is no second dispatch queue or worker.

A global `agent_notices.id` identifies one normal-poll operation. Acceptance
freezes the existing Telegram owner recipient, authenticated non-secret bot
account, rendered chunks, markup fallback and notice buttons. An immutable
notice source receipt, its shared outbound intent and diagnostic progress commit
in one transaction. A filtered receipt freezes the filter decision and inserts
no intent. An absent adapter/owner or account failure holds acceptance without
inventing delivery or a filtered result. Existing accepted sources are never
retargeted after the owner changes. Queued account mismatches retain their
original target under the shared worker's policy.

`im_bridge_notice_acceptances` has no foreign-key pin or expiry. The provider
worker never treats a poll retry as permission to resend an ambiguous operation.
A provider acknowledgement lost, successful prefix followed by an error, or
abandoned sending attempt remains uncertain; subsequent recipient messages may
proceed. Acceptance is not proof of delivery.

## Cutover and late commits

`im_bridge_notice_poll_state` owns one immutable legacy floor and a strict
import reason. A valid existing `notice_cursor.json` must be a non-boolean
integer >= 0: it is imported once, preserving that historical skip range.
Missing/corrupt state with existing committed history records
`legacy_history_unknown` and fixes the floor at the committed maximum. That
unknown old range is retained for manual inspection through explicit `/notice`;
it is never automatically replayed. Empty history records `no_history` at floor
zero during daemon startup before readiness, so a fresh install can accept its
first later notice even before the initial periodic poll.
Initialization does not fabricate receipts for old rows.

After cutover, eligibility is `id > legacy_floor` plus an anti-join against
normal-poll receipts. The maximum accepted ID and the JSON cursor are only
diagnostics/cache, never eligibility. A low ID reserved after cutover can commit
after a higher notice was accepted and still be found. IDs do not establish
transaction commit order. Old in-flight IDs below the initial cutover floor
remain part of the unknown legacy boundary; this is not a promise to recover
all historical gaps.

## Generation compatibility and remaining producers

The legacy daemon sends directly and only writes JSON cursor state. It cannot
read or obey these database source receipts. The new schema and transaction
gate cannot fence an old provider call: **mixed legacy and new IM generations
are not safe for either normal notices or the timeline Outbox cutover**.
Before enabling the new generation, the authorized operator must stop all old
IM daemons and drain their already-started provider calls, then enable the new
generation with the matching schema. Do not re-enable a legacy sending binary
as a rollback. This contribution performs no deployment.

New workers share the existing `4477` / `im-timeline-outbox` transaction gate
namespace; its historical name is deliberately preserved. Existing stored
manifest JSON keys and adapter kinds remain readable after internal vocabulary
renaming.

Explicit `/notice` listing and its replay callbacks retain their deliberate
immediate-send behavior in this slice. Ops `/send` fan-out, alert-transition
identity, chunk acknowledgement/recovery and retention/reconciliation remain
open under #4477. No new UI or client Outbox is introduced.
