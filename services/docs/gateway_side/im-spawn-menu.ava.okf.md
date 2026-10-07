---
type: doc
title: IM spawn menu identity
description: Creation event keys and the in-memory draft recovery boundary.
tags:
- im-bridge
- agent-lifecycle
---

# Spawn menu identity

`spawn_menu.py` forwards the inbound event key to `GatewayClient.spawn_agent`.
Telegram taps use `callback_query.id`, never the menu message ID shared by
separate taps. Weixin spawn commands scope provider message IDs by sender.
Existing Feishu message keys are preserved; unkeyed card callbacks mint a
fresh key; different events remain distinct creation intents.

Creation submissions remain single-attempt. An explicit caller may reuse a key
with the same payload to recover the server receipt; changed payloads conflict.
There is no automatic ambiguous retry or client outbox, and older gateways may
ignore the header until capability negotiation is implemented.

Drafts are cleared before submission and only live in memory. Event-key plumbing
does not preserve a draft after response loss or freeze it across daemon
restarts. Replaying a callback without its original selections cannot recover
the original payload; that recovery boundary needs separate domain work.
