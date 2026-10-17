---
type: doc
title: IM spawn menu identity
description: Creation event keys and the in-memory draft recovery boundary.
tags:
- im-bridge
- agent-lifecycle
---

# Spawn menu identity

`/spawn` is a pure menu (design locked with the user, 2026-08-03): text
arguments are ignored, and the menu progresses through model and effort
choices. Every layer carries a summary [Spawn] button ("Spawn: <model> /
<effort>", "Default" for anything unselected) so any layer completes. There is
no confirm step: tapping Spawn creates the agent immediately, and the receipt
offers a "Switch to #id" button. Effort choices use the selected model's
published options or the generic fallback when unavailable; choosing provider
default leaves the effort override unset.

`spawn_menu.py` forwards the inbound event key to `GatewayClient.spawn_agent`.
Telegram taps use `callback_query.id`, never the menu message ID shared by
separate taps. Weixin spawn commands scope provider message IDs by sender.
Existing Feishu message keys are preserved; unkeyed card callbacks mint a
fresh key; different events remain distinct creation intents.

Creation submissions remain single-attempt and send the `principal-v1` scope. An
explicit caller may reuse a key with the same payload to recover the server
receipt; changed payloads conflict. There is no automatic ambiguous retry or
client outbox.

Drafts are cleared before submission and only live in memory. Event-key plumbing
does not preserve a draft after response loss or freeze it across daemon
restarts. Replaying a callback without its original selections cannot recover
the original payload; that recovery boundary needs separate domain work.
