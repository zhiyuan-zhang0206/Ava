---
type: doc
title: "IM Outbound Retry Safety"
description: "Timeline durable acceptance, selection ownership and conservative whole-send recovery."
tags:
- im-bridge
- idempotency
---

# IM outbound retry safety

`push_watchdog.send_with_retry` and `IMBridgeCore.notify_user` retry only
`SendNotStartedError`, the adapter's explicit proof that the whole logical
message has no accepted or ambiguous chunks. Every other exception stops this
call without retrying the complete text. A ConnectError on chunk two does not
justify repeating chunk one. HTTP 5xx, read/write timeouts, unknown SDK errors
and partial delivery are not safe-retry evidence.

Telegram and Weixin sanitize pre-send ConnectError, ConnectTimeout and
PoolTimeout into this marker on the first chunk only. If an earlier chunk was
acknowledged, the public send converts that marker into an ordinary incomplete
send error. This also applies to the Telegram markup fallback: a confirmed
markup rejection may fall back to plain text, but cannot restart an accepted
prefix. Feishu makes no whole-send retry promise for unknown SDK errors.

Weixin creates a fresh client_id per new send/chunk and keeps that ID within
its confirmed stale-session rejection retry. A failed send no longer leaves a
chat/chunk-index cache that could give a different message the same identity.
Content equality or a five-minute window cannot prove two sends are one intent.

A proven unstarted send receives exactly one retry after bounded jitter:
`ImBridgeConfig.im_push_retry_backoff_seconds` + uniform jitter up to
`im_push_retry_jitter_seconds` (default 1.0s + U(0, 2.0s)). Failures emit im_push_failed; uncertain pushes log ERROR without retry. Owner notification
fan-out keeps its compatible per-channel error result and continues other
channels. Channels without an owner chat are skipped.

## Provider evidence and remaining boundary

The [official Telegram Bot API sendMessage parameters](https://core.telegram.org/bots/api#sendmessage)
return a Message after success but expose no client dedup key for this method
(checked 2026-10-07). This Bot API is distinct from Telegram's MTProto
messages.sendMessage random_id. We cannot infer safe Bot API replay from the
MTProto contract. No new iLink or Feishu duplicate-elimination guarantee is
claimed by this change; provider-specific identity and retention need their own
verified contract before enabling ambiguous retries.

## Timeline server Outbox

Timeline dialog uses `TimelineOutboxStore`, not the command watchdog send path.
`IMBridgeCore._push_snapshot` treats SSE as a wakeup and fetches the committed
Gateway timeline. A periodic committed-tail pull covers an SSE emitted before
checkpoint commit with no later event. `_accept_timeline` freezes adapter kind,
account, recipient, rendered chunks and Telegram's confirmed-rejection plain
fallback. The manifest stores no bot token, app secret or Weixin context token.
Authentication and ephemeral transport context remain adapter-owned.

Acceptance locks the recipient's `im_bridge_cursors` row and inserts
`im_bridge_outbound_intents` in the **same transaction** as cursor advancement.
Only a persistent message ID/block coordinate or explicit persisted inbound
ID/block qualifies a source. Positional UI anchors, timestamps and text hashes
never identify an outbound intent. A fresh unqualified source holds the cursor;
regular acceptance may commit its preceding qualified prefix. A replay batch
with an unqualified source is held as a whole. Historical qualification is not
silently repaired or replayed.

`im_bridge_outbound_replays` stores the immutable `/switch` invocation argument,
target and original accepted intent IDs, including an empty batch. A repeated
platform event recovers this batch even after more timeline items appear or the
old target disappears; changed arguments/targets conflict. Telegram callbacks
use the click ID, not the shared bot message ID. An internal call without a
stable event ID gets one UUID per logical call and makes no cross-restart
identity promise. Batch IDs are retained snapshots without queue-pinning FKs.

The cursor's `push_agent_id` also owns initialized chat selection; the JSON
switch file is a derived cache. Recipient mutexes order canonical reads and
cache application within a daemon; database selection fences reject late SSE
acceptance for a former target. Status clearing is compare-and-set. Recovery
rebuilds selection even after acceptance committed before its JSON cache write,
and an old A replay cannot undo a later B switch. Before account binding,
uninitialized legacy rows import the existing JSON choice once. A position for
a different former agent is cleared, not applied to the imported target. An
empty legacy position remains held (`push_initialized=false`); explicit empty
switch acceptance initializes the target so its first later output can send.
An absent legacy JSON choice clears rather than reviving a former selection.

Account identity comes from existing non-secret owners: Feishu app ID, Weixin
login account ID plus endpoint, and Telegram bot User ID returned by cached
[`getMe`](https://core.telegram.org/bots/api#getme). Identity failure holds
acceptance. Initial NULL account binding preserves a matching old position;
regular pushes cannot rebind a different account. Only explicit switch
acceptance can rebind. Queued intents for unavailable accounts keep their exact
original target and a fixed diagnostic; eligible accounts are filtered before
the worker's bounded stream selection so old records cannot starve new ones.

## Dispatch, uncertainty and retention

One whole-send runs per daemon. A PgBouncer transaction-pooling-compatible
`pg_try_advisory_xact_lock` gates each account/recipient stream on connection A.
**This transaction intentionally spans the network send.** Connection B uses
short transactions to recover abandoned `sending` rows to `uncertain`, commit a
new attempt token with `queued -> sending` **before** calling the provider, and
commit `sent`, `uncertain` or `failed` using the original token/status CAS.
No cursor or intent row lock spans the network. Worker enablement requires pool
`max_size >= 2` (the existing default); two leases/backends are needed during a
send. Try-lock avoids daemons waiting on each other's live claims. There is no
TTL claim stealing or session-level advisory lock.

Cancellation drains an already-started external call (including SDK thread
work) and short commit before releasing its gate. A hard process death releases
the gate and leaves a durable `sending` attempt. Losing the gate/backend or an
acknowledgement cannot prove no delivery: abandoned attempts become uncertain
and are **never automatically resent**. A successful prefix followed by any
ambiguous failure is uncertain. Only adapter `SendNotStartedError` proof of no
whole-send effect permits `failed`; this slice still does not retry that item.
A stale completion cannot overwrite a recovered outcome. Reasons/logs use
fixed classifications, never HTTP exception text containing credentials.

Uncertain records are retained while later recipient messages may proceed.
All intent identities and replay receipts are retained without automatic expiry
in this slice. `sent` means the adapter completed its whole accepted manifest;
this is not an exactly-once provider guarantee. A multi-chunk uncertain attempt
may have delivered any prefix; there are no persisted chunk acknowledgements,
resumable chunk retries or automatic reconciliation yet.

#4477 remains open for notice/ops producer identity and atomic acceptance,
provider capability verification, chunk acknowledgement/progress and an
operator-directed reconciliation/retention policy. Their existing immediate
send paths above remain separate. The pre-existing `state.outbox.jsonl` is the
user-to-Gateway inbound journal and is never used to dispatch provider output.
No client Outbox, resend UI or deployment is introduced. Acceptance is not proof
of delivery.
