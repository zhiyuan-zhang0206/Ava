---
type: doc
title: Weixin Ordinary Inbound Identity
description: "Qualified iLink source keys for ordinary chat, with explicit acceptance and cursor recovery gaps."
tags: []
---

# Weixin Ordinary Inbound Identity

`adapters/weixin.py` derives `weixin-chat-v1:` keys from the actual configured
provider endpoint, bot account, sender and provider message ID. Text is not
identity: deliberate identical messages with different IDs remain distinct. The
key passes through `InboundMessage.idempotency_key`, core ordinary chat and the
existing Gateway `/api/agents/{id}/messages` transactional inbound receipt.
For the same agent and request, a lost response followed by adapter/core restart
recovers the original inbound. Changed text or another agent under that legacy
key conflicts; this is not recovery of a changed chat selection.

Qualification accepts exact positive integers or ASCII decimal strings within
uint64 range, canonicalized as decimal without floating-point conversion.
Booleans, floats, zero, negative, overflow, whitespace, malformed and missing IDs
remain unqualified. Bot/account and sender must be nonempty strings. Provider
namespace uses HTTPS host/port/path with host case, default port and trailing
slash normalized; URLs with credentials, query, fragment or malformed port do
not qualify. No token or context token enters key material. The existing spawn
key derivation and in-memory seen heuristics remain unchanged.

Only ordinary plain text and provider voice transcripts qualify. All slash
commands, `spawn:` menu actions and `notice:` callbacks are excluded from this
chat key. A plain message intercepted by notice reply mode is not chat receipt
coverage: that owner consumes it before normal chat delivery and needs its own
operation/acceptance contract. There is no new schema, queue, worker, scope
negotiation, automatic retry policy, UI or client Outbox in this slice.

[Tencent's protocol types](https://github.com/Tencent/openclaw-weixin/blob/main/src/api/types.ts)
identify `message_id` as lossless uint64 and retain an opaque getUpdates cursor.
[The official client protocol description](https://github.com/Tencent/openclaw-weixin/blob/main/docs/protocol_zh_CN.md)
explicitly distinguishes client fields/behavior from the full server contract.
These sources do not establish globally unique event IDs, immutable same-ID
updates, indefinite replay retention or exactly-once sends. Conflicting same-ID
content therefore fails under the existing Gateway receipt rather than silently
being reinterpreted as new work. Missing qualification keeps legacy one-shot
behavior; content/time hashes cannot create a strong source identity.

The remaining acceptance gap is deliberate: `_seen` is a 300-second memory cache
marked before core; `handle_inbound` catches failures and returns no accepted
verdict; `_poll_once` can therefore advance `get_updates_buf` after failed work.
The global JSON cursor is not account-bound, and save errors can leave disk behind
memory. Existing inbound journal writes only after gateway failure and does not
freeze a source-to-agent route before the first HTTP attempt. Its pending entry
removal is not a retained source receipt. A later agent switch, reply-mode change,
account replacement, command failure or mixed batch is not solved by these keys.
Do not automatically replay all commands or claim a handled hint as acceptance.
Future work must establish immutable routing and per-item durable acceptance
before advancing seen/cursors, preserve uncertain command outcomes and bind the
cursor to its actual account/generation. Legacy Gateway receipt retention also
bounds replay; deleting an inbound must not be called permanent dedup coverage.
#4470 and #4477 remain open. No deployment is included.
