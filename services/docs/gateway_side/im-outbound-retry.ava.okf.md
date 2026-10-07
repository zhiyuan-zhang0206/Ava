---
type: doc
title: "IM Outbound Retry Safety"
description: "Unstarted-send proof, partial-send boundaries and provider uncertainty."
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
A later durable outbound record must supply actual logical message identity.

A proven unstarted send receives exactly one retry after bounded jitter:
`ImBridgeConfig.im_push_retry_backoff_seconds` + uniform jitter up to
`im_push_retry_jitter_seconds` (default 1.0s + U(0, 2.0s)). Its first failure logs
WARNING; a failed retry logs ERROR and emits im_push_failed. An uncertain push
logs ERROR without automatic retry and emits the same event. Owner notification
fan-out keeps its compatible per-channel error result and continues other
channels. NotImplementedError skips a channel with no owner chat.

## Provider evidence and remaining boundary

The [official Telegram Bot API sendMessage parameters](https://core.telegram.org/bots/api#sendmessage)
return a Message after success but expose no client dedup key for this method
(checked 2026-10-07). This Bot API is distinct from Telegram's MTProto
messages.sendMessage random_id. We cannot infer safe Bot API replay from the
MTProto contract. No new iLink or Feishu duplicate-elimination guarantee is
claimed by this change; provider-specific identity and retention need their own
verified contract before enabling ambiguous retries.

This is an immediate duplicate-prevention boundary, not durable outbound
recovery. Timeline watermarks still precede unrecorded provider sends, and
ops-alert producer retries can start a later notification call. Server outbound
intent, immutable chunk manifests, persisted acknowledgement IDs/progress,
worker claims, cursor-to-enqueue atomicity and inspectable uncertain outcomes
remain the #4477 domain work. No browser outbox is added. Operators must not
interpret a logged attempt or advanced watermark as proof of delivery.
